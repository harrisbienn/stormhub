"""Portable model-neutral export fixtures; no network, native reader or HEC."""

import copy
import json
from pathlib import Path

import pytest

from stormhub.met.forecast_precipitation import CONTRACT, from_hrrr, validate_export, verify_export, write_export
from stormhub.met.hrrr_acquisition import ACQUISITION_CONTRACT, descriptor
from stormhub.met.hrrr_normalization import FORCING_CONTRACT, validate_hour_tags
from stormhub.met.precipitation_features import (
    NORMALIZED_CONTRACT,
    SUPPORT_CONTRACT,
    PrecipitationFeatureError,
    canonical_bytes,
    digest,
    extract_features,
)

GRIB_FIXTURES = Path(__file__).parent / "fixtures/hrrr"


def _save(root, name, content):
    path = root / name
    path.write_bytes(content if isinstance(content, bytes) else canonical_bytes(content))
    return descriptor(path, root)


@pytest.fixture
def retained_hrrr(tmp_path):
    """Build deterministic retained-format evidence, not a decoded-data assertion."""
    raw, root = tmp_path / "raw", tmp_path / "bundle"
    raw.mkdir()
    root.mkdir()
    request = {
        "initialization": "2025-10-25T18:00:00Z",
        "hours": 2,
        "model_version": "hrrr-v4",
        "base_url": "https://example.test",
    }
    records, intervals = [], []
    for hour in (1, 2):
        content = (GRIB_FIXTURES / f"f{hour:02d}.grib2").read_bytes()
        message = _save(raw, f"f{hour:02d}.grib2", content)
        index = _save(raw, f"f{hour:02d}.idx", f"fixture-index-{hour}".encode())
        records.append(
            {
                "hour": hour,
                "url": f"https://example.test/f{hour:02d}.grib2",
                "etag": f'"revision-{hour}"',
                "object_size_bytes": len(content),
                "object_last_modified": None,
                "byte_range": [0, len(content) - 1],
                "observed_at": f"2025-10-25T18:{hour:02d}:00Z",
                "index": index,
                "message": message,
            }
        )
        tags = json.loads((GRIB_FIXTURES / f"f{hour:02d}-tags.json").read_bytes())
        intervals.append(
            {
                "hour": hour,
                "metadata": validate_hour_tags(tags, request["initialization"], hour),
                "grib_tags": tags,
                "volume_check": {"fixture": True},
            }
        )
    acquisition = {
        "contract_version": ACQUISITION_CONTRACT,
        "request": request,
        "records": records,
        "original_publication_time": None,
        "availability_status": "archive-retrieval-only",
    }
    acquisition["acquisition_id"] = digest(acquisition)
    grid = {
        "crs": "EPSG:5070",
        "shape": [1, 2],
        "origin": [0, 0],
        "cell_size_meters": 1000,
        "row_order": "south_to_north",
    }
    normalized = {
        "contract_version": NORMALIZED_CONTRACT,
        "grid": grid,
        "event_start": request["initialization"],
        "event_end": "2025-10-25T20:00:00Z",
        "interval_minutes": 60,
        "units": "mm",
        "measurement": "interval-depth",
        "values": [[3.0, None], [0.0, None]],
    }
    support = {
        "contract_version": SUPPORT_CONTRACT,
        "grid_sha256": digest(grid),
        "partition": None,
        "regions": [
            {
                "id": "basin",
                "kind": "basin",
                "area_m2": 2_000_000,
                "geometry_sha256": "a" * 64,
                "cells": [{"index": 0, "area_m2": 1_000_000}, {"index": 1, "area_m2": 1_000_000}],
            }
        ],
    }
    weights = {
        "method": "projected-cell-intersection-v1",
        "target_grid_sha256": digest(grid),
        "native_grid": {"crs": "EPSG:5070", "shape": [2, 2], "transform": [3000, 0, 0, 0, -3000, 6000]},
    }
    producer = {
        "name": "stormhub.hrrr-forcing",
        "version": "0.1.0",
        "revision": "b" * 40,
        "dirty": False,
        "code_sha256": "c" * 64,
        "dependencies": {name: "fixture" for name in ("rasterio", "numpy", "pyproj", "shapely")},
        "gdal": "fixture",
    }
    assets = {
        name: _save(root, name + ".json", content)
        for name, content in (
            ("normalized", normalized),
            ("support", support),
            ("weights", weights),
            ("acquisition", acquisition),
        )
    }
    forcing_id = digest(
        {
            "acquisition": acquisition["acquisition_id"],
            "normalized": assets["normalized"],
            "support": digest(support),
            "producer": producer,
        }
    )
    features = extract_features(
        normalized,
        source={
            "collection_id": "hrrr-conus-hourly-apcp",
            "item_id": forcing_id,
            "asset_key": "normalized-precipitation",
            "role": "incoming-precipitation",
            "sha256": assets["normalized"]["sha256"],
            "size_bytes": assets["normalized"]["size_bytes"],
        },
        support=support,
        extractor={
            "name": "stormhub.precipitation-features",
            "version": "0.1.0",
            "revision": "b" * 40,
            "dirty": False,
            "code_sha256": "d" * 64,
            "dependencies": {},
        },
    )
    assets["features"] = _save(root, features["feature_id"] + ".json", features)
    receipt = {
        "contract_version": FORCING_CONTRACT,
        "forcing_id": forcing_id,
        "request": request,
        "producer": producer,
        **assets,
        "feature_id": features["feature_id"],
        "intervals": intervals,
        "selected_target_cells": 2,
        "outside_support": "null-not-consumed",
        "availability": {
            "status": "archive-retrieval-only",
            "original_publication_time": None,
            "first_observed_at": records[0]["observed_at"],
            "complete_observed_at": records[-1]["observed_at"],
        },
        "applicability": {"forecast_eligible": False},
    }
    receipt["dataset_id"] = "forcing-dataset-" + digest(receipt)[:24]
    path = root / (receipt["dataset_id"] + ".json")
    path.write_bytes(canonical_bytes(receipt))
    return path, raw


def _rehash(export):
    export["export_id"] = (
        "forecast-precipitation-" + digest({key: value for key, value in export.items() if key != "export_id"})[:24]
    )
    return export


def _rehash_retained(forcing, receipt, *, feature_change=None):
    """Update all declared content identities so refusal must inspect semantics."""
    root = forcing.parent
    acquisition = json.loads((root / receipt["acquisition"]["href"]).read_bytes())
    support = json.loads((root / receipt["support"]["href"]).read_bytes())
    receipt["forcing_id"] = digest(
        {
            "acquisition": acquisition["acquisition_id"],
            "normalized": receipt["normalized"],
            "support": digest(support),
            "producer": receipt["producer"],
        }
    )
    features = json.loads((root / receipt["features"]["href"]).read_bytes())
    features["source"]["item_id"] = receipt["forcing_id"]
    if feature_change:
        features["source"].update(feature_change)
    features["feature_id"] = (
        "precipitation-features-" + digest({key: value for key, value in features.items() if key != "feature_id"})[:24]
    )
    receipt["features"] = _save(root, features["feature_id"] + ".json", features)
    receipt["feature_id"] = features["feature_id"]
    receipt["dataset_id"] = (
        "forcing-dataset-" + digest({key: value for key, value in receipt.items() if key != "dataset_id"})[:24]
    )
    forcing.write_bytes(canonical_bytes(receipt))


def test_hrrr_projection_preserves_bytes_identity_missingness_and_availability(retained_hrrr, tmp_path):
    """A wrapper adds identity without rewriting legacy artifacts or inventing clocks."""
    forcing, raw = retained_hrrr
    before = {path: path.read_bytes() for root in (forcing.parent, raw) for path in root.iterdir()}
    export = from_hrrr(forcing, raw_root=raw)
    expected = json.loads((Path(__file__).parent / "fixtures/forecast_precipitation/hrrr-export-v1.json").read_bytes())
    assert export == expected
    assert export["contract_version"] == CONTRACT
    assert (export["provider"], export["model"], export["model_version"], export["member"]) == (
        "noaa",
        "hrrr",
        "hrrr-v4",
        "deterministic",
    )
    assert export["available_at"] == "2025-10-25T18:02:00Z"
    assert export["availability"]["original_publication_time"] is None
    assert export["source_revision"]["provider_revision"] is None
    assert [interval["missing_cells"] for interval in export["intervals"]] == [1, 1]
    assert export["intervals"][1]["values_sha256"] == digest([0.0, None])
    assert not export["forecast_eligible"]
    verify_export(export, bundle_root=forcing.parent, raw_root=raw)
    output = write_export(export, tmp_path / "exports", bundle_root=forcing.parent, raw_root=raw)
    assert json.loads(output.read_bytes()) == export
    assert write_export(export, output.parent, bundle_root=forcing.parent, raw_root=raw) == output
    assert from_hrrr(forcing, raw_root=raw) == export
    assert before == {path: path.read_bytes() for path in before}


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider", "unregistered"),
        ("model", "gfs"),
        ("model_version", "hrrr-v99"),
        ("member", "member-01"),
        ("product", "blend"),
        ("adapter_version", "99.0"),
        ("units", "mm/hour"),
        ("interval_seconds", True),
        ("forecast_eligible", True),
        ("contract_version", "unregistered/2"),
    ],
)
def test_unknown_product_or_semantics_refuse_before_output(retained_hrrr, tmp_path, field, value):
    """A new identity or unit cannot silently fall through an HRRR adapter."""
    forcing, raw = retained_hrrr
    export = from_hrrr(forcing, raw_root=raw)
    export[field] = value
    _rehash(export)
    output = tmp_path / "refused"
    with pytest.raises(PrecipitationFeatureError):
        write_export(export, output, bundle_root=forcing.parent, raw_root=raw)
    assert not output.exists()


@pytest.mark.parametrize(
    "change",
    [
        "cumulative",
        "reset",
        "gap",
        "overlap",
        "duplicate",
        "missing",
        "available",
        "publication",
        "revision",
        "grid",
        "extra",
    ],
)
def test_semantic_drift_cannot_be_hidden_by_rehashing(retained_hrrr, change):
    """Clocks, accumulation, revision and grid provenance remain independently checked."""
    forcing, raw = retained_hrrr
    export = from_hrrr(forcing, raw_root=raw)
    if change == "cumulative":
        export["normalization"]["source_measurement"] = "cumulative-depth"
    elif change == "reset":
        export["intervals"][1]["accumulation"]["reset_at"] = export["initialization"]
    elif change == "gap":
        export["intervals"][1]["start"] = "2025-10-25T19:01:00Z"
    elif change == "overlap":
        export["intervals"][1]["accumulation"]["start"] = export["initialization"]
    elif change == "duplicate":
        export["intervals"][1] = copy.deepcopy(export["intervals"][0])
    elif change == "missing":
        export["intervals"].pop()
    elif change == "available":
        export["available_at"] = export["initialization"]
    elif change == "publication":
        export["availability"]["original_publication_time"] = export["initialization"]
    elif change == "revision":
        export["source_revision"]["provider_revision"] = "latest"
    elif change == "grid":
        export["source_grid"]["transform"][0] = 9000
    else:
        export["cycle_id"] = "consumer-owned"
    with pytest.raises(PrecipitationFeatureError):
        validate_export(_rehash(export))


@pytest.mark.parametrize("asset", ["normalized", "support", "weights", "features", "acquisition", "raw"])
def test_corrupt_retained_asset_refuses_before_publication(retained_hrrr, tmp_path, asset):
    """Every referenced evidence family is authenticated before any export write."""
    forcing, raw = retained_hrrr
    export = from_hrrr(forcing, raw_root=raw)
    if asset == "raw":
        path = raw / export["intervals"][0]["raw"]["message"]["href"]
    elif asset == "acquisition":
        path = forcing.parent / export["provenance"]["acquisition"]["href"]
    else:
        path = forcing.parent / export[asset]["href"]
    path.write_bytes(b"corrupt")
    output = tmp_path / "refused"
    with pytest.raises(PrecipitationFeatureError, match="checksum|size"):
        write_export(export, output, bundle_root=forcing.parent, raw_root=raw)
    assert not output.exists()


@pytest.mark.parametrize("href", ["../escape", "/absolute", "C:/absolute", "file:stream", "folder\\file", "a//b"])
def test_unsafe_asset_references_refuse(retained_hrrr, href):
    """Cross-platform reference validation precedes reading or publishing."""
    forcing, raw = retained_hrrr
    export = from_hrrr(forcing, raw_root=raw)
    export["normalized"]["href"] = href
    with pytest.raises(PrecipitationFeatureError, match="portable"):
        validate_export(_rehash(export))


def test_rehashed_metadata_substitution_does_not_pass_owner_verification(retained_hrrr):
    """Plausible metadata with a new self-hash still must match the retained source."""
    forcing, raw = retained_hrrr
    export = from_hrrr(forcing, raw_root=raw)
    export["intervals"][0]["missing_cells"] = 0
    _rehash(export)
    validate_export(export)
    with pytest.raises(PrecipitationFeatureError, match="retained owner"):
        verify_export(export, bundle_root=forcing.parent, raw_root=raw)


def test_conflicting_export_is_preserved(retained_hrrr, tmp_path):
    """Immutable export retry never overwrites conflicting retained evidence."""
    forcing, raw = retained_hrrr
    export = from_hrrr(forcing, raw_root=raw)
    output = write_export(export, tmp_path / "exports", bundle_root=forcing.parent, raw_root=raw)
    output.write_bytes(b"prior conflicting evidence")
    with pytest.raises(PrecipitationFeatureError, match="differs"):
        write_export(export, output.parent, bundle_root=forcing.parent, raw_root=raw)
    assert output.read_bytes() == b"prior conflicting evidence"


def test_explicit_product_policy_can_extend_envelope_without_hrrr_assumptions(retained_hrrr, monkeypatch):
    """A synthetic registered policy exercises the extension seam, not a new provider."""
    from stormhub.met import forecast_precipitation as module

    forcing, raw = retained_hrrr
    export = from_hrrr(forcing, raw_root=raw)
    product = ("test-fixture", "synthetic", "1", "deterministic", "accumulation", "1")
    export.update(dict(zip(module._PRODUCT_FIELDS, product)))
    export["source_grid"] = {"definition": "test-only-unstructured-source"}
    export["source_grid_sha256"] = digest(export["source_grid"])
    export["normalization"] = {"method": "fixture-explicit-difference"}
    export["provenance"] = {"fixture_evidence": "not-HRRR"}
    export["source_revision"] = {"fixture_revision": "one"}
    for interval in export["intervals"]:
        interval["accumulation"]["start"] = export["initialization"]
        interval["accumulation"]["reset_at"] = export["initialization"]
        interval["raw"] = {"fixture_raw": interval["raw"]["message"]}
    _rehash(export)
    with pytest.raises(PrecipitationFeatureError, match="unsupported provider"):
        validate_export(export)
    calls = []

    def semantics(value):
        assert value["normalization"] == {"method": "fixture-explicit-difference"}
        assert value["provenance"] == {"fixture_evidence": "not-HRRR"}
        calls.append("semantics")

    def retained_bytes(value, bundle_root, raw_root):
        assert bundle_root == forcing.parent and raw_root == raw
        calls.append("bytes")

    monkeypatch.setattr(module, "_PRODUCT_POLICIES", {product: (semantics, retained_bytes)})
    verify_export(export, bundle_root=forcing.parent, raw_root=raw)
    assert calls == ["semantics", "bytes"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("revision", None),
        ("revision", "unrecorded"),
        ("dirty", "false"),
        ("code_sha256", "bad"),
        ("dependencies", {}),
        ("dependencies", {name: "" for name in ("rasterio", "numpy", "pyproj", "shapely")}),
        ("gdal", ""),
        ("extra", "unowned"),
    ],
)
def test_rehashed_retained_producer_cannot_drop_execution_identity(retained_hrrr, field, value):
    """Legacy content hashes do not excuse malformed producer provenance."""
    forcing, raw = retained_hrrr
    receipt = json.loads(forcing.read_bytes())
    if value is None:
        del receipt["producer"][field]
    else:
        receipt["producer"][field] = value
    _rehash_retained(forcing, receipt)
    with pytest.raises(PrecipitationFeatureError, match="producer"):
        from_hrrr(forcing, raw_root=raw)


@pytest.mark.parametrize(
    "field,value",
    [
        ("role", "target-precipitation"),
        ("collection_id", "historical-storms"),
        ("asset_key", "target-dss"),
    ],
)
def test_rehashed_retained_features_cannot_impersonate_forecast_source(retained_hrrr, field, value):
    """An otherwise valid historical feature cannot be relabeled incoming HRRR."""
    forcing, raw = retained_hrrr
    receipt = json.loads(forcing.read_bytes())
    _rehash_retained(forcing, receipt, feature_change={field: value})
    with pytest.raises(PrecipitationFeatureError, match="features do not bind"):
        from_hrrr(forcing, raw_root=raw)


def test_changed_retrieval_has_separate_identity_and_availability(retained_hrrr, tmp_path):
    """Revised object evidence creates another export without replacing the prior bundle."""
    import shutil

    forcing, raw = retained_hrrr
    original = from_hrrr(forcing, raw_root=raw)
    revision_root = tmp_path / "revised"
    shutil.copytree(forcing.parent, revision_root)
    revised_forcing = revision_root / forcing.name
    receipt = json.loads(revised_forcing.read_bytes())
    acquisition = json.loads((revision_root / receipt["acquisition"]["href"]).read_bytes())
    acquisition["records"][-1].update(etag='"new-provider-object"', observed_at="2025-10-25T18:05:00Z")
    acquisition["acquisition_id"] = digest(
        {key: value for key, value in acquisition.items() if key != "acquisition_id"}
    )
    receipt["acquisition"] = _save(revision_root, "acquisition.json", acquisition)
    receipt["availability"]["complete_observed_at"] = "2025-10-25T18:05:00Z"
    _rehash_retained(revised_forcing, receipt)
    revised = from_hrrr(revised_forcing, raw_root=raw)
    assert revised["export_id"] != original["export_id"]
    assert revised["source_revision"] != original["source_revision"]
    assert revised["available_at"] == "2025-10-25T18:05:00Z"
    verify_export(original, bundle_root=forcing.parent, raw_root=raw)
