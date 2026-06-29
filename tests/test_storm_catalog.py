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

    assert storm_dss_filename(item, output_resolution_km=1) == "r007_20200102T0600_24h_aorc_shg1k.dss"


def test_add_storm_dss_files_writes_portable_assets_and_manifest(tmp_path, monkeypatch) -> None:
    """Write relative DSS item assets and a collection-level manifest."""
    catalog = make_catalog("24hr-events")
    collection = catalog.get_child("24hr-events")
    collection.add_item(
        pystac.Item(
            id="1",
            geometry={"type": "Point", "coordinates": [0.5, 0.5]},
            bbox=[0.5, 0.5, 0.5, 0.5],
            datetime=None,
            properties={},
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
    catalog.normalize_hrefs(str(tmp_path))
    catalog.save(catalog_type=pystac.CatalogType.SELF_CONTAINED)

    def fake_dss_writer(output_path, *_args, **_kwargs) -> None:
        Path(output_path).write_bytes(b"test-dss")

    monkeypatch.setattr("stormhub.met.storm_catalog.noaa_zarr_to_dss", fake_dss_writer)
    dss_dir = tmp_path / "24hr-events" / "dss"
    add_storm_dss_files(
        catalog,
        collection_id="24hr-events",
        dss_output_dir=str(dss_dir),
        output_resolution_km=1,
    )

    saved_item = pystac.Item.from_file(str(tmp_path / "24hr-events" / "1" / "1.json"))
    assert saved_item.assets["dss"].href == "../dss/r001_20200102T0000_24h_aorc_shg1k.dss"
    assert saved_item.assets["dss"].roles == ["data"]

    saved_collection = pystac.Collection.from_file(str(tmp_path / "24hr-events" / "collection.json"))
    assert saved_collection.assets["dss_manifest"].href == "dss/dss-manifest.csv"
    manifest = (dss_dir / "dss-manifest.csv").read_text(encoding="utf-8")
    assert "r001_20200102T0000_24h_aorc_shg1k.dss" in manifest
