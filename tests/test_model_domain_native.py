"""Complete model-cell rainfall proof from small retained GRIB2 fixtures."""

import copy
import json

import pytest

pytest.importorskip("numpy")
rasterio = pytest.importorskip("rasterio")
pytest.importorskip("shapely")
pytest.importorskip("pyproj")

from stormhub.met import forecast_precipitation
from stormhub.met.hrrr_acquisition import acquire, descriptor
from stormhub.met.model_domain_precipitation import (
    from_forecast,
    new_domain,
    normalize_hrrr,
    verify_export,
    write_export,
)
from stormhub.met.precipitation_features import PrecipitationFeatureError, canonical_bytes, digest, extract_features
from test_hrrr_acquisition import REQUEST, fake_provider
from test_hrrr_native import FIXTURES, target


@pytest.fixture
def retained_domain(tmp_path):
    """Decode fixture bytes using an in-process transport, never a provider."""
    transport, _ = fake_provider()
    raw = tmp_path / "raw"
    acquisition = acquire(REQUEST, raw, transport=transport)
    with rasterio.open(FIXTURES / "f01.grib2") as dataset:
        grid = target(dataset)
    domain = new_domain(grid=grid, required_cells=list(range(16)), model_domain_sha256="c" * 64)
    root = tmp_path / "bundle"
    export = normalize_hrrr(
        acquisition, raw, domain=domain, output=root, revision="a" * 40, observed_end=REQUEST["initialization"]
    )
    return export, domain, root, raw, acquisition


def test_complete_domain_volume_area_time_and_replay(retained_domain, tmp_path):
    """Two hours cover all 16 cells and conserve 270000 m3 over 36 square km."""
    export, domain, root, raw, acquisition = retained_domain
    verify_export(export, expected_domain=domain, bundle_root=root, raw_root=raw)
    assert [i["target_volume_m3"] for i in export["intervals"]] == pytest.approx([90000, 180000])
    assert sum(i["target_volume_m3"] for i in export["intervals"]) == pytest.approx(270000)
    assert all(i["required_cell_count"] == 16 and i["area_m2"] == 36_000_000 for i in export["intervals"])
    assert export["intervals"][0]["end"] == export["intervals"][1]["start"]
    assert export["source"]["intervals"][0]["accumulation"]["reset_at"] is None
    before = {p: p.read_bytes() for folder in (root, raw) for p in folder.rglob("*") if p.is_file()}
    retry = normalize_hrrr(
        acquisition, raw, domain=domain, output=root, revision="a" * 40, observed_end=REQUEST["initialization"]
    )
    assert retry == export
    path = write_export(export, tmp_path / "receipts", expected_domain=domain, bundle_root=root, raw_root=raw)
    assert json.loads(path.read_bytes()) == export
    assert write_export(export, path.parent, expected_domain=domain, bundle_root=root, raw_root=raw) == path
    assert all(p.read_bytes() == content for p, content in before.items())


def test_mask_is_declared_not_inferred_and_seam_is_explicit(retained_domain, tmp_path):
    """A valid smaller declared model mask cannot substitute for a larger one."""
    _, domain, _, raw, acquisition = retained_domain
    small = new_domain(grid=domain["grid"], required_cells=[0, 1, 4, 5], model_domain_sha256="d" * 64)
    root = tmp_path / "smaller"
    export = normalize_hrrr(acquisition, raw, domain=small, output=root, revision="a" * 40)
    verify_export(export, expected_domain=small, bundle_root=root, raw_root=raw)
    normalized = json.loads((root / export["source"]["normalized"]["href"]).read_bytes())
    assert normalized["values"][0][:4] == [3, 3, None, None]
    assert all(i["outside_domain_cells"] == 12 for i in export["intervals"])
    with pytest.raises(PrecipitationFeatureError, match="expected model domain"):
        verify_export(export, expected_domain=domain, bundle_root=root, raw_root=raw)
    with pytest.raises(PrecipitationFeatureError, match="seam"):
        normalize_hrrr(
            acquisition,
            raw,
            domain=small,
            output=tmp_path / "bad-seam",
            revision="a" * 40,
            observed_end="2025-10-25T19:00:00Z",
        )
    assert not (tmp_path / "bad-seam").exists()


def _alter_normalized(export, root, mutate):
    """Rebuild every F02 hash and feature so native proof must detect false data."""
    source = export["source"]
    receipt_path = root / source["provenance"]["forcing"]["href"]
    receipt = json.loads(receipt_path.read_bytes())
    normalized_path = root / receipt["normalized"]["href"]
    normalized = json.loads(normalized_path.read_bytes())
    mutate(normalized)
    normalized_path.write_bytes(canonical_bytes(normalized))
    receipt["normalized"] = descriptor(normalized_path, root)
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
    prior = json.loads((root / receipt["features"]["href"]).read_bytes())
    feature_source = {
        **prior["source"],
        "item_id": receipt["forcing_id"],
        "sha256": receipt["normalized"]["sha256"],
        "size_bytes": receipt["normalized"]["size_bytes"],
    }
    features = extract_features(
        normalized, source=feature_source, support=support, extractor=prior["extractor"], definition=prior["definition"]
    )
    feature_path = root / (features["feature_id"] + ".json")
    feature_path.write_bytes(canonical_bytes(features))
    receipt["features"] = descriptor(feature_path, root)
    receipt["feature_id"] = features["feature_id"]
    receipt["dataset_id"] = "forcing-dataset-" + digest({k: v for k, v in receipt.items() if k != "dataset_id"})[:24]
    receipt_path = root / (receipt["dataset_id"] + ".json")
    receipt_path.write_bytes(canonical_bytes(receipt))
    return forecast_precipitation.from_hrrr(receipt_path, raw_root=root.parent / "raw")


@pytest.mark.parametrize(
    "replacement,match", [(None, "required rainfall"), (99, "native source"), (0, "native source")]
)
def test_rehashed_missing_or_wrong_rain_cannot_pass_native_proof(retained_domain, replacement, match):
    """Missing cells and invented dry/wet values fail even with valid F02 hashes."""
    export, domain, root, raw, _ = retained_domain
    source = _alter_normalized(export, root, lambda data: data["values"][0].__setitem__(0, replacement))
    forecast_precipitation.verify_export(source, bundle_root=root, raw_root=raw)
    with pytest.raises(PrecipitationFeatureError, match=match):
        from_forecast(source, domain=domain, bundle_root=root, raw_root=raw, revision="a" * 40)


@pytest.mark.parametrize("asset", ["raw", "weights", "normalized", "support"])
def test_corrupt_retained_bytes_refuse_before_publication(retained_domain, tmp_path, asset):
    """Corrupt numerical or spatial evidence never produces an accepted export."""
    export, domain, root, raw, _ = retained_domain
    reference = export["source"]["intervals"][0]["raw"]["message"] if asset == "raw" else export["source"][asset]
    (raw if asset == "raw" else root).joinpath(reference["href"]).write_bytes(b"corrupted")
    with pytest.raises(PrecipitationFeatureError):
        write_export(export, tmp_path / "refused", expected_domain=domain, bundle_root=root, raw_root=raw)
    assert not (tmp_path / "refused").exists()


def test_rehashed_omitted_remap_cell_refuses(retained_domain):
    """An authenticated matrix must cover exactly all required cells."""
    export, domain, root, raw, _ = retained_domain
    source = copy.deepcopy(export["source"])
    path = root / source["weights"]["href"]
    weights = json.loads(path.read_bytes())
    weights["target_cells"].pop()
    path.write_bytes(canonical_bytes(weights))
    receipt_path = root / source["provenance"]["forcing"]["href"]
    receipt = json.loads(receipt_path.read_bytes())
    receipt["weights"] = descriptor(path, root)
    receipt["dataset_id"] = "forcing-dataset-" + digest({k: v for k, v in receipt.items() if k != "dataset_id"})[:24]
    receipt_path = root / (receipt["dataset_id"] + ".json")
    receipt_path.write_bytes(canonical_bytes(receipt))
    source = forecast_precipitation.from_hrrr(receipt_path, raw_root=raw)
    with pytest.raises(PrecipitationFeatureError, match="remapping matrix"):
        from_forecast(source, domain=domain, bundle_root=root, raw_root=raw, revision="a" * 40)


def test_wrong_roots_grid_and_required_extent_refuse(retained_domain, tmp_path):
    """No search fallback, accidental grid swap or partial native footprint."""
    export, domain, root, raw, acquisition = retained_domain
    with pytest.raises(PrecipitationFeatureError):
        verify_export(export, expected_domain=domain, bundle_root=tmp_path, raw_root=raw)
    with pytest.raises(PrecipitationFeatureError):
        verify_export(export, expected_domain=domain, bundle_root=root, raw_root=tmp_path)
    grid = copy.deepcopy(domain["grid"])
    grid["origin"][0] -= 1500
    shifted = new_domain(grid=grid, required_cells=list(range(16)), model_domain_sha256="d" * 64)
    with pytest.raises(PrecipitationFeatureError, match="partition"):
        normalize_hrrr(acquisition, raw, domain=shifted, output=tmp_path / "shifted", revision="a" * 40)
