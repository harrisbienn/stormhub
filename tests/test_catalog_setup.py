"""Exercise catalog setup using small real geometries and no AORC service."""

from copy import deepcopy
import json
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import shape

from stormhub.met import catalog_setup as setup
from stormhub.met import storm_catalog
from stormhub.utils import sha256_file


def polygon(left=0, right=1):
    """Return a small polygon mapping without scientific source assets."""
    return {"type": "Polygon", "coordinates": [[[left, 0], [right, 0], [right, 1], [left, 1], [left, 0]]]}


def write_features(path, geometries):
    """Write a GeoJSON fixture with a declared WGS84 CRS."""
    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [{"type": "Feature", "properties": {}, "geometry": geom} for geom in geometries],
            }
        )
    )


@pytest.fixture
def settings(tmp_path):
    """Create adjacent watershed features and a containing region."""
    write_features(tmp_path / "watershed.json", [polygon(0, 1), polygon(1, 2)])
    write_features(tmp_path / "region.json", [polygon(-1, 3)])
    return {
        "catalog_id": "example-v2",
        "watershed": {
            "id": "watershed-v2",
            "geometry_file": "watershed.json",
            "description": "watershed",
            "source_sha256": sha256_file(tmp_path / "watershed.json"),
        },
        "transposition_region": {
            "id": "region-v2",
            "geometry_file": "region.json",
            "description": "region",
            "source_sha256": sha256_file(tmp_path / "region.json"),
        },
        "params": {"check_every_n_hours": 6},
    }


def snapshot(directory):
    """Capture file bytes and timestamps to detect writes during reuse or refusal."""
    return {
        p.relative_to(directory).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in directory.rglob("*")
        if p.is_file()
    }


def prepare(settings, tmp_path, **kwargs):
    """Prepare fixture inputs in a dedicated catalog root."""
    return setup.prepare_catalog_inputs(settings, tmp_path, tmp_path / "catalogs", **kwargs)


def test_union_and_crs_metadata(settings, tmp_path):
    """Preserve every adjacent feature while returning one WGS84 polygon."""
    domains = setup.prepare_catalog_domains(settings, tmp_path)
    watershed = domains["watershed"]
    assert watershed.geometry.equals(shape(polygon(0, 2)))
    assert watershed.source_features == 2
    assert watershed.summary()["prepared_crs"] == "EPSG:4326"
    assert domains["transposition_region"].geometry.covers(watershed.geometry)


def test_reprojects_declared_crs(tmp_path):
    """Convert geographic source coordinates through their actual source CRS."""
    path = tmp_path / "projected.geojson"
    value = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "EPSG:3857"}},
        "features": [{"type": "Feature", "properties": {}, "geometry": polygon(0, 111319)}],
    }
    path.write_text(json.dumps(value))
    domain = setup.prepare_domain(path, sha256_file(path))
    assert domain.source_crs == "EPSG:3857"
    assert domain.geometry.bounds[2] == pytest.approx(1, abs=1e-5)


@pytest.mark.parametrize(
    "geometries",
    [
        [polygon(0, 1), polygon(2, 3)],
        [{"type": "Point", "coordinates": [0, 0]}],
        [{"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [0, 1], [1, 0], [0, 0]]]}],
        [],
    ],
)
def test_unusable_geometry_has_no_destination(settings, tmp_path, geometries):
    """Refuse disconnected, non-polygonal, invalid and empty source features."""
    path = tmp_path / "watershed.json"
    write_features(path, geometries)
    settings["watershed"]["source_sha256"] = sha256_file(path)
    with pytest.raises(ValueError):
        prepare(settings, tmp_path)
    assert not (tmp_path / "catalogs").exists()


def test_source_checksum_mismatch(settings, tmp_path):
    """Fail before writes when supplied source bytes change."""
    (tmp_path / "watershed.json").write_text("changed")
    with pytest.raises(ValueError, match="checksum changed"):
        prepare(settings, tmp_path)
    assert not (tmp_path / "catalogs").exists()


def test_missing_crs(settings, tmp_path, monkeypatch):
    """Do not guess a CRS for a reader result without one."""
    monkeypatch.setattr(setup.gpd, "read_file", lambda path: gpd.GeoDataFrame(geometry=[shape(polygon())]))
    with pytest.raises(ValueError, match="declared CRS"):
        prepare(settings, tmp_path)


def test_read_mutation(settings, tmp_path, monkeypatch):
    """Detect a source changing after its initial checksum was checked."""
    original = setup.gpd.read_file

    def read_and_change(path):
        frame = original(path)
        path.write_text(path.read_text() + " ")
        return frame

    monkeypatch.setattr(setup.gpd, "read_file", read_and_change)
    with pytest.raises(ValueError, match="changed during"):
        prepare(settings, tmp_path)


@pytest.mark.parametrize("catalog_id", ["../old", "C:/old", "CON", "old."])
def test_unsafe_identity(settings, tmp_path, catalog_id):
    """Keep generated paths within the requested catalog directory."""
    settings["catalog_id"] = catalog_id
    with pytest.raises(ValueError, match="portable"):
        prepare(settings, tmp_path)


def test_reuse_is_read_only_and_errors_are_actionable(settings, tmp_path):
    """Reuse exact inputs without rewriting them, and refuse default overwrite."""
    config_path = prepare(settings, tmp_path)
    before = snapshot(config_path.parent)
    with pytest.raises(FileExistsError, match="reuse"):
        prepare(settings, tmp_path)
    assert prepare(settings, tmp_path, existing="reuse") == config_path
    assert snapshot(config_path.parent) == before
    changed = deepcopy(settings)
    changed["params"]["check_every_n_hours"] = 24
    with pytest.raises(ValueError, match="differs"):
        prepare(changed, tmp_path, existing="reuse")
    assert snapshot(config_path.parent) == before


@pytest.mark.parametrize("filename", ["params-config.json", "inputs/watershed-v2.geojson"])
def test_missing_inputs_are_not_silently_repaired(settings, tmp_path, filename):
    """Keep partial preparation explicit instead of filling uncertain state."""
    config_path = prepare(settings, tmp_path)
    (config_path.parent / filename).unlink()
    before = snapshot(config_path.parent)
    with pytest.raises(ValueError, match="Missing"):
        prepare(settings, tmp_path, existing="reuse")
    assert snapshot(config_path.parent) == before


def test_creation_failure_can_retry_without_overwriting_completed_catalog(settings, tmp_path, monkeypatch):
    """Retry AORC failure and reopen the completed real STAC tree without writes."""
    config_path = prepare(settings, tmp_path)

    def unavailable(*args):
        raise ConnectionError("AORC unavailable")

    monkeypatch.setattr(storm_catalog, "valid_spaces_item", unavailable)
    with pytest.raises(ConnectionError, match="AORC"):
        setup.create_prepared_catalog(config_path)
    assert not (config_path.parent / "catalog.json").exists()
    with pytest.raises(FileExistsError, match="Partial"):
        setup.create_prepared_catalog(config_path)
    monkeypatch.setattr(storm_catalog, "valid_spaces_item", lambda *args: shape(polygon(-1, 3)))
    catalog = setup.create_prepared_catalog(config_path, existing="reuse")
    assert catalog.id == settings["catalog_id"]
    events = config_path.parent / "24hr-events"
    events.mkdir()
    (events / "retained-output.dss").write_bytes(b"preserved event output")
    before = snapshot(config_path.parent)
    monkeypatch.setattr(storm_catalog, "valid_spaces_item", unavailable)
    assert setup.create_prepared_catalog(config_path, existing="reuse").id == catalog.id
    with pytest.raises(FileExistsError, match="exists"):
        setup.create_prepared_catalog(config_path)
    assert snapshot(config_path.parent) == before


def test_prepared_geometry_tamper_prevents_creation(settings, tmp_path):
    """Do not create a base catalog from changed prepared geometry."""
    config_path = prepare(settings, tmp_path)
    path = config_path.parent / "inputs/watershed-v2.geojson"
    path.write_text(path.read_text() + " ")
    with pytest.raises(ValueError, match="checksums differ"):
        setup.create_prepared_catalog(config_path)


def test_partial_catalog_with_event_products_is_refused(settings, tmp_path):
    """A missing root JSON never permits rebuilding over event products."""
    config_path = prepare(settings, tmp_path)
    (config_path.parent / "24hr-events").mkdir()
    before = snapshot(config_path.parent)
    with pytest.raises(ValueError, match="unexpected products"):
        setup.create_prepared_catalog(config_path, existing="reuse")
    assert snapshot(config_path.parent) == before


def test_invalid_existing_mode_is_rejected(settings, tmp_path):
    """A misspelled mode or boolean cannot enable replacement."""
    with pytest.raises(ValueError, match="existing must"):
        prepare(settings, tmp_path, existing="overwrite")


@pytest.mark.parametrize("change", ["geometry", "external-link", "missing-valid-region"])
def test_completed_catalog_must_match_local_domains(settings, tmp_path, monkeypatch, change):
    """Reject changed base geometry and broken/external links without rewriting."""
    config_path = prepare(settings, tmp_path)
    monkeypatch.setattr(storm_catalog, "valid_spaces_item", lambda *args: shape(polygon(-1, 3)))
    setup.create_prepared_catalog(config_path)
    catalog_path = config_path.parent / "catalog.json"
    if change == "geometry":
        domain_path = config_path.parent / "hydro_domains/watershed-v2.json"
        item = json.loads(domain_path.read_text())
        item["geometry"] = polygon(0, 1)
        domain_path.write_text(json.dumps(item))
    else:
        catalog = json.loads(catalog_path.read_text())
        if change == "external-link":
            for link in catalog["links"]:
                if link.get("title") == "Watershed":
                    link["href"] = "https://example.invalid/other.json"
        else:
            catalog["links"] = [link for link in catalog["links"] if link.get("title") != "Valid Transposition Region"]
        catalog_path.write_text(json.dumps(catalog))
    before = snapshot(config_path.parent)
    with pytest.raises(ValueError, match="Existing catalog"):
        setup.create_prepared_catalog(config_path, existing="reuse")
    assert snapshot(config_path.parent) == before


def test_plot_returns_customizable_axes(settings, tmp_path):
    """Keep presentation customization in the notebook without inline helpers."""
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt

    ax = setup.plot_catalog_domains(setup.prepare_catalog_domains(settings, tmp_path), title="Example")
    try:
        assert ax.get_title() == "Example"
        assert ax.get_xlabel() == "Longitude"
    finally:
        plt.close(ax.figure)
