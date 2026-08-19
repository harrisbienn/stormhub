"""Tests for authenticated storm ensemble-feature exports."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pystac
import pytest
from shapely.geometry import Polygon
import xarray as xr

from stormhub.met.ensemble_features import (
    FEATURE_TABLE_SCHEMA,
    compute_precipitation_features,
    export_ensemble_feature_table,
    main,
)
from stormhub.utils import sha256_file, sha256_multihash


def make_precipitation() -> xr.DataArray:
    """Create a four-hour target-grid precipitation fixture in SHG coordinates."""
    precipitation = xr.DataArray(
        np.asarray(
            [
                [[1.0, 1.0], [1.0, 1.0]],
                [[2.0, 0.0], [2.0, 0.0]],
                [[0.0, 3.0], [0.0, 3.0]],
                [[1.0, 1.0], [1.0, 1.0]],
            ]
        ),
        dims=("time", "y", "x"),
        coords={
            "time": pd.date_range("2020-01-01T01:00:00Z", periods=4, freq="h"),
            "y": [1500.0, 500.0],
            "x": [500.0, 1500.0],
        },
    )
    precipitation.rio.set_spatial_dims(x_dim="x", y_dim="y", inplace=True)
    precipitation.rio.write_crs("EPSG:5070", inplace=True)
    precipitation.rio.write_transform(inplace=True)
    return precipitation


def test_compute_precipitation_features_covers_required_categories() -> None:
    """Derive accumulation, intensity, spatial, and temporal event descriptors."""
    features = compute_precipitation_features(make_precipitation())

    assert features["accumulation_mean_mm"] == 4.5
    assert features["accumulation_max_mm"] == 5.0
    assert features["maximum_hourly_mean_mm"] == 1.5
    assert features["precipitation_centroid_x_m"] == pytest.approx(1055.55555556)
    assert features["precipitation_centroid_y_m"] == 1000.0
    assert features["maximum_precipitation_x_m"] == 1500.0
    assert features["maximum_precipitation_y_m"] == 1500.0
    assert features["spatial_accumulation_cv"] == pytest.approx(0.11111111)
    assert features["peak_time_fraction"] == 0.75
    assert features["cumulative_fraction_at_25pct"] == pytest.approx(0.22222222)
    assert features["cumulative_fraction_at_50pct"] == pytest.approx(0.44444444)
    assert features["cumulative_fraction_at_75pct"] == pytest.approx(0.77777778)


def test_compute_precipitation_features_uses_explicit_target_zones() -> None:
    """Use zone-average event totals for the spatial-distribution metric."""
    zones = gpd.GeoDataFrame(
        {"zone_id": ["west", "east"]},
        geometry=[
            Polygon([(0, 0), (1000, 0), (1000, 2000), (0, 2000)]),
            Polygon([(1000, 0), (2000, 0), (2000, 2000), (1000, 2000)]),
        ],
        crs="EPSG:5070",
    )

    features = compute_precipitation_features(make_precipitation(), zones=zones)

    assert features["spatial_accumulation_cv"] == pytest.approx(0.11111111)


def make_export_catalog(root: Path, *, item_count: int = 1) -> tuple[Path, Path]:
    """Create a self-contained catalog and authenticated target-DSS manifest."""
    catalog = pystac.Catalog(id="test-catalog", description="Test catalog")
    extent = pystac.Extent(
        pystac.SpatialExtent([[0.0, 0.0, 1.0, 1.0]]),
        pystac.TemporalExtent([[datetime(2020, 1, 1, tzinfo=timezone.utc), None]]),
    )
    collection = pystac.Collection(id="4hr-events", description="Test events", extent=extent)
    catalog.add_child(collection)
    source_domain = pystac.Item(
        id="source-domain",
        geometry={"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]},
        bbox=[0, 0, 1, 1],
        datetime=datetime(2020, 1, 1, tzinfo=timezone.utc),
        properties={},
    )
    watershed = pystac.Item(
        id="target-watershed",
        geometry={"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]},
        bbox=[0, 0, 1, 1],
        datetime=datetime(2020, 1, 1, tzinfo=timezone.utc),
        properties={"hydro_domain:type": "watershed"},
    )
    catalog.add_item(source_domain)
    catalog.add_item(watershed)
    for rank in range(1, item_count + 1):
        start = datetime(2020, 1, rank, tzinfo=timezone.utc)
        collection.add_item(
            pystac.Item(
                id=str(rank),
                geometry={"type": "Point", "coordinates": [0.5, 0.5]},
                bbox=[0.5, 0.5, 0.5, 0.5],
                datetime=None,
                start_datetime=start,
                end_datetime=start + timedelta(hours=4),
                properties={"aorc:transform": {"a": 1.0, "b": 0.0, "c": 0.0, "d": 0.0, "e": 1.0, "f": 0.0}},
            )
        )

    catalog.normalize_hrefs(str(root))
    dss_dir = root / "4hr-events" / "dss"
    dss_dir.mkdir(parents=True)
    rows = []
    for rank, item in enumerate(collection.get_items(), start=1):
        item_href = Path(item.get_self_href())
        target_path = dss_dir / f"r{rank:03d}_target.dss"
        target_path.write_bytes(f"target-{rank}".encode())
        checksum = sha256_multihash(sha256_file(target_path))
        item.add_asset(
            "dss-target",
            pystac.Asset(
                href=Path("..", "dss", target_path.name).as_posix(),
                media_type="application/x-dss",
                roles=["data", "target"],
                extra_fields={
                    "stormhub:spatial_role": "target",
                    "stormhub:source_domain_id": "source-domain",
                    "stormhub:target_watershed_id": "target-watershed",
                    "stormhub:duration_hours": 4,
                    "stormhub:time_step": "PT1H",
                    "stormhub:time_zone": "UTC",
                    "stormhub:output_crs": "EPSG:5070",
                    "stormhub:output_resolution_m": 1000,
                    "stormhub:target_buffer_km": 0,
                    "stormhub:x_offset_m": 1000,
                    "stormhub:y_offset_m": -2000,
                    "stormhub:validation_status": "passed",
                    "file:size": target_path.stat().st_size,
                    "file:checksum": checksum,
                    "proj:code": "EPSG:5070",
                    "proj:shape": [2, 2],
                    "proj:bbox": [0.0, 0.0, 2000.0, 2000.0],
                },
            ),
        )
        rows.append(
            {
                "item_id": item.id,
                "rank": rank,
                "spatial_role": "target",
                "start_datetime": item.properties["start_datetime"],
                "end_datetime": item.properties["end_datetime"],
                "duration_hours": 4,
                "asset_key": "dss-target",
                "dss_filename": target_path.name,
                "dss_href": f"dss/{target_path.name}",
                "checksum": checksum,
                "target_watershed_id": "target-watershed",
                "x_offset_m": 1000,
                "y_offset_m": -2000,
                "validation_status": "passed",
            }
        )
        assert item_href.parent.name == item.id

    manifest_path = dss_dir / "dss-manifest.csv"
    pd.DataFrame(rows).to_csv(manifest_path, index=False)
    collection.add_asset(
        "dss_manifest",
        pystac.Asset(
            href="dss/dss-manifest.csv",
            media_type="text/csv",
            roles=["metadata"],
        ),
    )
    catalog.save(catalog_type=pystac.CatalogType.SELF_CONTAINED)
    return root / "catalog.json", manifest_path


def fixture_provider(
    _catalog: pystac.Catalog,
    _item: pystac.Item,
    _asset: pystac.Asset,
) -> tuple[xr.DataArray, dict]:
    """Return deterministic target precipitation without accessing NOAA AORC."""
    return make_precipitation(), {"x_offset_m": 1000, "y_offset_m": -2000}


def canonical_sha256(payload: dict) -> str:
    """Hash a feature table using its documented canonical JSON rule."""
    content = dict(payload)
    content.pop("table_sha256", None)
    return hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def test_export_ensemble_feature_table_authenticates_catalog_and_manifest(tmp_path) -> None:
    """Bind complete candidate records to current STAC, manifest, and DSS content."""
    catalog_path, manifest_path = make_export_catalog(tmp_path / "catalog")
    output_path = tmp_path / "exports" / "features.json"

    payload = export_ensemble_feature_table(
        catalog_path,
        collection_id="4hr-events",
        manifest_path=manifest_path,
        study_id="test-study",
        output_path=output_path,
        precipitation_provider=fixture_provider,
    )

    assert output_path.exists()
    assert payload["schema"] == FEATURE_TABLE_SCHEMA
    assert payload["candidate_count"] == 1
    assert payload["duration_hours"] == 4
    assert payload["watershed_id"] == "target-watershed"
    assert payload["source"]["owner_repository"] == "stormhub"
    assert payload["source"]["catalog_sha256"] == sha256_file(tmp_path / "catalog" / "4hr-events" / "collection.json")
    assert payload["source"]["manifest_sha256"] == sha256_file(manifest_path)
    assert payload["table_sha256"] == canonical_sha256(payload)
    assert payload["records"][0]["target_dss"]["checksum"].startswith("1220")
    assert payload["records"][0]["features"]["offset_distance_m"] == pytest.approx(2236.0679775)
    assert all(
        "\\" not in href
        for href in (
            payload["source"]["catalog_href"],
            payload["source"]["manifest_href"],
            payload["records"][0]["stac_item_href"],
            payload["records"][0]["target_dss"]["href"],
        )
    )

    repeated = export_ensemble_feature_table(
        catalog_path,
        collection_id="4hr-events",
        manifest_path=manifest_path,
        study_id="test-study",
        output_path=output_path,
        precipitation_provider=fixture_provider,
    )
    assert repeated == payload


def test_export_ensemble_feature_table_rejects_truncated_manifest(tmp_path) -> None:
    """Fail closed when the authoritative target manifest omits a collection Item."""
    catalog_path, manifest_path = make_export_catalog(tmp_path / "catalog", item_count=2)
    manifest = pd.read_csv(manifest_path)
    manifest.iloc[:1].to_csv(manifest_path, index=False)

    with pytest.raises(ValueError, match="cover the collection exactly"):
        export_ensemble_feature_table(
            catalog_path,
            collection_id="4hr-events",
            manifest_path=manifest_path,
            study_id="test-study",
            output_path=tmp_path / "features.json",
            precipitation_provider=fixture_provider,
        )


def test_export_ensemble_feature_table_authenticates_zone_file(tmp_path) -> None:
    """Hash a single-file target-zone source and record the zonal spatial basis."""
    catalog_path, manifest_path = make_export_catalog(tmp_path / "catalog")
    zones_path = tmp_path / "zones.gpkg"
    gpd.GeoDataFrame(
        {"zone_id": ["west", "east"]},
        geometry=[
            Polygon([(0, 0), (1000, 0), (1000, 2000), (0, 2000)]),
            Polygon([(1000, 0), (2000, 0), (2000, 2000), (1000, 2000)]),
        ],
        crs="EPSG:5070",
    ).to_file(zones_path)

    payload = export_ensemble_feature_table(
        catalog_path,
        collection_id="4hr-events",
        manifest_path=manifest_path,
        study_id="test-study",
        output_path=tmp_path / "features.json",
        zones_path=zones_path,
        precipitation_provider=fixture_provider,
    )

    assert payload["source"]["zones_sha256"] == sha256_file(zones_path)
    assert payload["spatial_distribution_basis"] == {"type": "target_zones", "zone_count": 2}


def test_export_ensemble_feature_table_rejects_tampered_dss(tmp_path) -> None:
    """Fail closed when target DSS bytes no longer match the authenticated asset."""
    catalog_path, manifest_path = make_export_catalog(tmp_path / "catalog")
    target_path = tmp_path / "catalog" / "4hr-events" / "dss" / "r001_target.dss"
    target_path.write_bytes(b"tampered")

    with pytest.raises(ValueError, match="checksum does not match file content"):
        export_ensemble_feature_table(
            catalog_path,
            collection_id="4hr-events",
            manifest_path=manifest_path,
            study_id="test-study",
            output_path=tmp_path / "features.json",
            precipitation_provider=fixture_provider,
        )


def test_ensemble_feature_cli_passes_explicit_inputs(monkeypatch, capsys, tmp_path) -> None:
    """Expose the package exporter through a Windows-friendly console command."""
    captured = {}

    def fake_export(catalog, **kwargs):
        captured["catalog"] = catalog
        captured.update(kwargs)
        return {"candidate_count": 460, "table_sha256": "a" * 64}

    monkeypatch.setattr("stormhub.met.ensemble_features.export_ensemble_feature_table", fake_export)
    output = tmp_path / "features.json"

    exit_code = main(
        [
            "catalog.json",
            "--collection-id",
            "24hr-events",
            "--manifest",
            "dss-manifest.csv",
            "--study-id",
            "deloutre",
            "--zones",
            "subbasins.geojson",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    assert captured == {
        "catalog": "catalog.json",
        "collection_id": "24hr-events",
        "manifest_path": "dss-manifest.csv",
        "study_id": "deloutre",
        "output_path": str(output),
        "zones_path": "subbasins.geojson",
    }
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "passed"
    assert summary["candidate_count"] == 460
