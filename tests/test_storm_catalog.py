"""Tests for storm catalog collection selection."""

from datetime import datetime, timezone
from pathlib import Path

import pystac
import pytest

from stormhub.met.storm_catalog import add_storm_dss_files, get_events_collection, storm_dss_filename


def make_collection(collection_id: str) -> pystac.Collection:
    """Create a minimal STAC collection for selection tests."""
    extent = pystac.Extent(
        pystac.SpatialExtent([[0.0, 0.0, 1.0, 1.0]]),
        pystac.TemporalExtent([[datetime(2020, 1, 1, tzinfo=timezone.utc), None]]),
    )
    return pystac.Collection(
        id=collection_id,
        description=f"{collection_id} test collection",
        extent=extent,
        license="proprietary",
    )


def make_catalog(*collection_ids: str) -> pystac.Catalog:
    """Create a catalog containing the requested collections."""
    catalog = pystac.Catalog(id="test-catalog", description="Test catalog")
    for collection_id in collection_ids:
        catalog.add_child(make_collection(collection_id))
    return catalog


def test_get_events_collection_uses_only_event_collection() -> None:
    """Select the sole events collection while ignoring other collections."""
    catalog = make_catalog("reference-data", "24hr-events")

    assert get_events_collection(catalog).id == "24hr-events"


def test_get_events_collection_selects_requested_collection() -> None:
    """Select an explicitly requested collection from a multi-duration catalog."""
    catalog = make_catalog("24hr-events", "48hr-events", "72hr-events")

    assert get_events_collection(catalog, collection_id="24hr-events").id == "24hr-events"


def test_get_events_collection_requires_id_when_ambiguous() -> None:
    """Require a collection ID when more than one events collection exists."""
    catalog = make_catalog("24hr-events", "48hr-events")

    with pytest.raises(ValueError, match="Multiple events collections"):
        get_events_collection(catalog)


def test_get_events_collection_reports_available_ids() -> None:
    """Report valid collection IDs when the requested collection is absent."""
    catalog = make_catalog("24hr-events", "72hr-events")

    with pytest.raises(ValueError, match="48hr-events.*24hr-events.*72hr-events"):
        get_events_collection(catalog, collection_id="48hr-events")


def test_storm_dss_filename_includes_event_identity() -> None:
    """Include rank, start hour, duration, source, and resolution in DSS filenames."""
    item = pystac.Item(
        id="7",
        geometry={"type": "Point", "coordinates": [0.5, 0.5]},
        bbox=[0.5, 0.5, 0.5, 0.5],
        datetime=None,
        properties={},
        start_datetime=datetime(2020, 1, 2, 6, tzinfo=timezone.utc),
        end_datetime=datetime(2020, 1, 3, 6, tzinfo=timezone.utc),
    )

    assert storm_dss_filename(item, output_resolution_km=1) == "r007_20200102T0600_24h_aorc_shg1k_source.dss"


def make_dss_catalog(root: Path) -> pystac.Catalog:
    """Create a saved storm catalog suitable for DSS export tests."""
    catalog = make_catalog("24hr-events")
    collection = catalog.get_child("24hr-events")
    collection.add_item(
        pystac.Item(
            id="1",
            geometry={"type": "Point", "coordinates": [0.5, 0.5]},
            bbox=[0.5, 0.5, 0.5, 0.5],
            datetime=None,
            properties={
                "aorc:transform": {"a": 1.0, "b": 0.0, "c": 0.25, "d": 0.0, "e": 1.0, "f": 0.25}
            },
            start_datetime=datetime(2020, 1, 2, tzinfo=timezone.utc),
            end_datetime=datetime(2020, 1, 3, tzinfo=timezone.utc),
        )
    )
    catalog.add_item(
        pystac.Item(
            id="transposition-domain",
            geometry={
                "type": "Polygon",
                "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
            },
            bbox=[0, 0, 1, 1],
            datetime=datetime(2020, 1, 1, tzinfo=timezone.utc),
            properties={},
        )
    )
    catalog.add_item(
        pystac.Item(
            id="watershed-domain",
            geometry={
                "type": "Polygon",
                "coordinates": [[[0, 0], [0.5, 0], [0.5, 0.5], [0, 0.5], [0, 0]]],
            },
            bbox=[0, 0, 0.5, 0.5],
            datetime=datetime(2020, 1, 1, tzinfo=timezone.utc),
            properties={"hydro_domain:type": "watershed"},
        )
    )
    catalog.normalize_hrefs(str(root))
    catalog.save(catalog_type=pystac.CatalogType.SELF_CONTAINED)
    return catalog


def test_add_storm_dss_files_writes_portable_assets_and_manifest(tmp_path, monkeypatch) -> None:
    """Write relative DSS item assets and a collection-level manifest."""
    catalog = make_dss_catalog(tmp_path)

    def fake_dss_writer(output_path, *_args, **_kwargs) -> None:
        Path(output_path).write_bytes(b"test-dss")

    monkeypatch.setattr("stormhub.met.storm_catalog.noaa_zarr_to_dss", fake_dss_writer)
    dss_dir = tmp_path / "24hr-events" / "dss"
    result = add_storm_dss_files(
        catalog,
        collection_id="24hr-events",
        output_resolution_km=1,
    )

    assert result["status"] == "passed"
    assert result["requested_count"] == 1
    assert result["succeeded_count"] == 1
    assert result["failed_count"] == 0
    assert result["successful_items"][0]["item_id"] == "1"
    assert result["successful_items"][0]["validation_status"] == {"source": "not_run"}
    assert result["manifest_href"] == "dss/dss-manifest.csv"

    saved_item = pystac.Item.from_file(str(tmp_path / "24hr-events" / "1" / "1.json"))
    source_asset = saved_item.assets["dss-source"]
    assert source_asset.href == "../dss/r001_20200102T0000_24h_aorc_shg1k_source.dss"
    assert source_asset.roles == ["data", "source"]
    assert source_asset.extra_fields["stormhub:spatial_role"] == "source"
    assert source_asset.extra_fields["stormhub:translation_method"] == "none"
    assert source_asset.extra_fields["proj:code"] == "EPSG:5070"

    saved_collection = pystac.Collection.from_file(str(tmp_path / "24hr-events" / "collection.json"))
    assert saved_collection.assets["dss_manifest"].href == "dss/dss-manifest.csv"
    manifest = (dss_dir / "dss-manifest.csv").read_text(encoding="utf-8")
    assert "r001_20200102T0000_24h_aorc_shg1k_source.dss" in manifest
    assert "source" in manifest


def test_add_storm_dss_files_records_target_translation(tmp_path, monkeypatch) -> None:
    """Write distinct source and target assets with projected translation metadata."""
    catalog = make_dss_catalog(tmp_path)
    translation = {
        "method": "shg-cell-snap",
        "direction": "source-to-target",
        "source_center_x": 10000.0,
        "source_center_y": 20000.0,
        "target_center_x": 5000.0,
        "target_center_y": 8000.0,
        "raw_x_offset_m": -5100.0,
        "raw_y_offset_m": -11900.0,
        "x_offset_m": -5000,
        "y_offset_m": -12000,
        "x_offset_cells": -5,
        "y_offset_cells": -12,
        "x_snap_residual_m": -100.0,
        "y_snap_residual_m": 100.0,
        "spatial_validation": {
            "PRECIPITATION": {
                "status": "passed",
                "values_preserved": True,
                "target_rows": 100,
                "target_columns": 120,
                "target_bounds": [0.0, 0.0, 120000.0, 100000.0],
            }
        },
        "dss_validation": {
            "source": {"status": "passed"},
            "target": {"status": "passed"},
        },
    }

    def fake_product_writer(output_dss_paths, **_kwargs):
        for output_path in output_dss_paths.values():
            Path(output_path).write_bytes(b"test-dss")
        return translation

    monkeypatch.setattr("stormhub.met.storm_catalog.noaa_zarr_to_dss_products", fake_product_writer)
    dss_dir = tmp_path / "24hr-events" / "dss"
    result = add_storm_dss_files(
        catalog,
        collection_id="24hr-events",
        dss_output_dir=str(dss_dir),
        output_resolution_km=1,
        output_modes=("source", "target"),
        target_buffer_km=5,
    )

    assert result["status"] == "passed"
    assert result["successful_items"][0]["validation_status"] == {
        "source": "passed",
        "target": "passed",
    }

    saved_item = pystac.Item.from_file(str(tmp_path / "24hr-events" / "1" / "1.json"))
    assert set(saved_item.assets).issuperset({"dss-source", "dss-target"})
    target_asset = saved_item.assets["dss-target"]
    assert target_asset.href == "../dss/r001_20200102T0000_24h_aorc_shg1k_target.dss"
    assert target_asset.roles == ["data", "target"]
    assert target_asset.extra_fields["stormhub:target_watershed_id"] == "watershed-domain"
    assert target_asset.extra_fields["stormhub:x_offset_m"] == -5000
    assert target_asset.extra_fields["stormhub:y_offset_cells"] == -12
    assert target_asset.extra_fields["stormhub:validation_status"] == "passed"
    assert target_asset.extra_fields["proj:shape"] == [100, 120]
    assert saved_item.assets["dss-validation"].href == "1.dss-validation.json"
    validation_path = tmp_path / "24hr-events" / "1" / "1.dss-validation.json"
    assert validation_path.exists()
    assert '"values_preserved": true' in validation_path.read_text(encoding="utf-8")

    manifest = (dss_dir / "dss-manifest.csv").read_text(encoding="utf-8")
    assert "dss-source" in manifest
    assert "dss-target" in manifest
    assert "target" in manifest
    assert "passed" in manifest


def test_add_storm_dss_files_reports_item_failures(tmp_path, monkeypatch) -> None:
    """Return an actionable failure summary while still writing the collection manifest."""
    catalog = make_dss_catalog(tmp_path)

    def failing_writer(*_args, **_kwargs) -> None:
        raise RuntimeError("AORC unavailable")

    monkeypatch.setattr("stormhub.met.storm_catalog.noaa_zarr_to_dss", failing_writer)

    result = add_storm_dss_files(catalog, collection_id="24hr-events")

    assert result["status"] == "failed"
    assert result["requested_count"] == 1
    assert result["succeeded_count"] == 0
    assert result["failed_count"] == 1
    assert result["failed_items"] == [
        {
            "item_id": "1",
            "error_type": "RuntimeError",
            "error": "AORC unavailable",
        }
    ]
    assert Path(result["manifest_path"]).exists()


def test_add_storm_dss_files_reports_validation_failures(tmp_path, monkeypatch) -> None:
    """Treat a written but invalid DSS product as a failed export item."""
    catalog = make_dss_catalog(tmp_path)

    def invalid_product_writer(output_dss_paths, **_kwargs):
        for output_path in output_dss_paths.values():
            Path(output_path).write_bytes(b"invalid-dss")
        return {
            "method": "shg-cell-snap",
            "direction": "source-to-target",
            "source_center_x": 10000.0,
            "source_center_y": 20000.0,
            "target_center_x": 5000.0,
            "target_center_y": 8000.0,
            "raw_x_offset_m": -5100.0,
            "raw_y_offset_m": -11900.0,
            "x_offset_m": -5000,
            "y_offset_m": -12000,
            "x_offset_cells": -5,
            "y_offset_cells": -12,
            "x_snap_residual_m": -100.0,
            "y_snap_residual_m": 100.0,
            "spatial_validation": {
                "PRECIPITATION": {
                    "status": "failed",
                    "values_preserved": False,
                    "target_rows": 100,
                    "target_columns": 120,
                    "target_bounds": [0.0, 0.0, 120000.0, 100000.0],
                }
            },
            "dss_validation": {"target": {"status": "passed"}},
        }

    monkeypatch.setattr(
        "stormhub.met.storm_catalog.noaa_zarr_to_dss_products",
        invalid_product_writer,
    )

    result = add_storm_dss_files(
        catalog,
        collection_id="24hr-events",
        output_modes=("target",),
    )

    assert result["status"] == "failed"
    assert result["succeeded_count"] == 0
    assert result["failed_count"] == 1
    assert result["failed_items"][0]["error_type"] == "DSSValidationError"
    assert result["failed_items"][0]["validation_status"] == {"target": "failed"}
