"""Tests for publishing hydraulic scenario runs into a STAC Catalog."""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pystac
import pytest
from pydantic import ValidationError

from stormhub.scenarios import (
    ExecutionSpec,
    FailureDetails,
    HydraulicModel,
    PrecipitationInput,
    QualityStatus,
    QualitySummary,
    RunStatus,
    ScenarioRun,
    ScenarioRunSpec,
    SoftwareProvenance,
    StacAssetReference,
    WatershedReference,
    file_reference_from_path,
    geometry_sha256,
    load_scenario_run,
    new_scenario_run,
    publish_scenario_run,
)
from stormhub.utils import sha256_file, sha256_from_checksum

START = datetime(2020, 1, 2, tzinfo=timezone.utc)


def relative_to_manifest(path: Path, manifest_path: Path) -> str:
    """Return a portable fixture href relative to its run manifest."""
    return Path(os.path.relpath(path.resolve(), start=manifest_path.resolve().parent)).as_posix()


def make_catalog_and_watershed(root: Path) -> tuple[pystac.Catalog, pystac.Item, Path]:
    """Create the catalog, watershed, and source precipitation Item fixtures."""
    catalog = pystac.Catalog(id="lwi-region3", description="LWI Region 3 scenarios")
    catalog.normalize_hrefs(str(root / "catalog"))
    catalog.save(catalog_type=pystac.CatalogType.SELF_CONTAINED)

    geometry = {
        "type": "Polygon",
        "coordinates": [[[-93, 32], [-92, 32], [-92, 33], [-93, 33], [-93, 32]]],
    }
    watershed = pystac.Item(
        id="lwi-r3-huc12-domain",
        geometry=geometry,
        bbox=[-93, 32, -92, 33],
        datetime=START,
        properties={"hydro_domain:type": "watershed"},
    )
    watershed_path = root / "catalog" / "hydro_domains" / "lwi-r3-huc12-domain.json"
    watershed.set_self_href(str(watershed_path))
    watershed.save_object(include_self_link=False)

    precipitation = pystac.Item(
        id="1",
        geometry={"type": "Point", "coordinates": [-92.5, 32.5]},
        bbox=[-92.5, 32.5, -92.5, 32.5],
        datetime=None,
        properties={},
        start_datetime=START,
        end_datetime=START + timedelta(hours=24),
        collection="24hr-events",
    )
    precipitation_path = root / "catalog" / "24hr-events" / "1" / "1.json"
    precipitation.set_self_href(str(precipitation_path))
    precipitation.save_object(include_self_link=False)
    return catalog, watershed, precipitation_path


def make_terminal_run(
    root: Path,
    watershed: pystac.Item,
    precipitation_path: Path,
    *,
    run_id: str = "scenario-001",
    status: RunStatus = RunStatus.SUCCEEDED,
) -> tuple[ScenarioRun, Path, Path]:
    """Build a terminal run and its checksum-pinned local artifacts."""
    manifest_path = root / "work" / run_id / "scenario-run.json"
    dss_path = root / "catalog" / "24hr-events" / "dss" / "event-target.dss"
    model_path = root / "models" / "lwi-r3-ras.zip"
    output_path = root / "outputs" / run_id / "wse.tif"
    for path in (dss_path, model_path, output_path):
        path.parent.mkdir(parents=True, exist_ok=True)
    dss_path.write_bytes(b"watershed precipitation")
    model_path.write_bytes(b"HEC-RAS model package")
    output_path.write_bytes(b"water surface elevation")

    model_package = file_reference_from_path(
        model_path,
        manifest_path,
        asset_key="hydraulic-model",
        media_type="application/zip",
        roles=["data", "model"],
    )
    output = file_reference_from_path(
        output_path,
        manifest_path,
        asset_key="wse",
        media_type="image/tiff; application=geotiff; profile=cloud-optimized",
        roles=["data", "hydraulic-output"],
        metadata={"proj:code": "EPSG:5070"},
    )
    spec = ScenarioRunSpec(
        watershed=WatershedReference(
            watershed_id=watershed.id,
            item_href=relative_to_manifest(Path(watershed.get_self_href()), manifest_path),
            geometry_sha256=geometry_sha256(watershed.geometry),
        ),
        precipitation=PrecipitationInput(
            source_aorc=StacAssetReference(
                item_href=relative_to_manifest(precipitation_path, manifest_path),
                collection_id="24hr-events",
                item_id="1",
                asset_key="AORC",
                asset_href="s3://noaa-nws-aorc-v1-1-1km/2020.zarr",
            ),
            transposed_dss=StacAssetReference(
                item_href=relative_to_manifest(precipitation_path, manifest_path),
                collection_id="24hr-events",
                item_id="1",
                asset_key="dss-target",
                asset_href=relative_to_manifest(dss_path, manifest_path),
                sha256=sha256_file(dss_path),
            ),
            event_start=START,
            event_end=START + timedelta(hours=24),
            duration_hours=24,
            rank=1,
        ),
        hydraulic_model=HydraulicModel(
            model_id="lwi-r3-ras",
            model_version="2026.07",
            engine_version="6.6",
            plan_name="forecast-plan",
            package=model_package,
        ),
        execution=ExecutionSpec(
            runner="windows-hec-ras-worker",
            software=SoftwareProvenance(
                repository="https://example.com/flood-model-runner.git",
                revision="0123456789abcdef",
                operating_system="Windows Server 2025",
                python_version="3.12.10",
            ),
        ),
    )
    planned = new_scenario_run(spec, run_id=run_id, created_at=START)
    payload = planned.model_dump()
    payload.update(
        {
            "status": status,
            "started_at": START + timedelta(minutes=1),
            "completed_at": START + timedelta(minutes=31),
        }
    )
    if status == RunStatus.SUCCEEDED:
        payload["outputs"] = [output.model_dump()]
        payload["quality"] = QualitySummary(status=QualityStatus.PASSED).model_dump()
    else:
        payload["failure"] = FailureDetails(
            error_type="ras_compute_error",
            message="HEC-RAS computation failed",
            retryable=True,
        ).model_dump()
    return ScenarioRun.model_validate(payload), manifest_path, output_path


def test_publish_successful_run_creates_portable_catalog_tree(tmp_path: Path) -> None:
    """Publish inputs, outputs, manifest, properties, and provenance links."""
    catalog, watershed, precipitation_path = make_catalog_and_watershed(tmp_path)
    run, manifest_path, _ = make_terminal_run(tmp_path, watershed, precipitation_path)

    item = publish_scenario_run(catalog, run, manifest_path, watershed)

    item_path = tmp_path / "catalog" / "hydraulic-scenario-runs" / run.run_id / f"{run.run_id}.json"
    collection_path = tmp_path / "catalog" / "hydraulic-scenario-runs" / "collection.json"
    assert item_path.exists()
    assert collection_path.exists()
    assert load_scenario_run(manifest_path) == run

    saved_item = pystac.Item.from_file(str(item_path))
    assert set(saved_item.assets) == {"scenario-run", "dss-target", "hydraulic-model", "wse"}
    assert saved_item.assets["wse"].extra_fields["proj:code"] == "EPSG:5070"
    assert saved_item.assets["wse"].href.startswith("../../../outputs/")
    assert saved_item.properties["stormhub:run_status"] == "succeeded"
    assert saved_item.properties["stormhub:quality_status"] == "passed"
    assert any(link.rel == pystac.RelType.DERIVED_FROM for link in saved_item.links)
    assert any(link.rel == "related" for link in saved_item.links)
    assert "https://stac-extensions.github.io/file/" in " ".join(saved_item.stac_extensions)
    assert "https://stac-extensions.github.io/projection/" in " ".join(saved_item.stac_extensions)

    manifest_checksum = saved_item.assets["scenario-run"].extra_fields["file:checksum"]
    assert sha256_from_checksum(manifest_checksum) == sha256_file(manifest_path)
    reloaded = pystac.Catalog.from_file(str(tmp_path / "catalog" / "catalog.json"))
    assert reloaded.get_child("hydraulic-scenario-runs").get_item(run.run_id) is not None
    assert item.id == run.run_id


def test_publish_failed_run_preserves_failure_provenance(tmp_path: Path) -> None:
    """Publish failed terminal runs so operators can discover and audit them."""
    catalog, watershed, precipitation_path = make_catalog_and_watershed(tmp_path)
    run, manifest_path, _ = make_terminal_run(
        tmp_path,
        watershed,
        precipitation_path,
        run_id="scenario-failed",
        status=RunStatus.FAILED,
    )

    item = publish_scenario_run(catalog, run, manifest_path, watershed)

    assert item.properties["stormhub:run_status"] == "failed"
    assert item.properties["stormhub:failure"] == {
        "error_type": "ras_compute_error",
        "retryable": True,
    }
    assert "wse" not in item.assets


def test_publisher_rejects_duplicates_and_changed_outputs(tmp_path: Path) -> None:
    """Protect immutable publication identity and detect post-run tampering."""
    catalog, watershed, precipitation_path = make_catalog_and_watershed(tmp_path)
    run, manifest_path, output_path = make_terminal_run(tmp_path, watershed, precipitation_path)
    publish_scenario_run(catalog, run, manifest_path, watershed)

    with pytest.raises(ValueError, match="already published"):
        publish_scenario_run(catalog, run, manifest_path, watershed)

    replaced = publish_scenario_run(catalog, run, manifest_path, watershed, overwrite=True)
    assert replaced.properties["stormhub:specification_sha256"] == run.specification_sha256

    second, second_manifest, second_output = make_terminal_run(
        tmp_path,
        watershed,
        precipitation_path,
        run_id="scenario-002",
    )
    second_output.write_bytes(b"changed after the completed manifest")
    with pytest.raises(ValueError, match="Size mismatch|Checksum mismatch"):
        publish_scenario_run(catalog, second, second_manifest, watershed)

    assert output_path.exists()


def test_contract_rejects_duplicate_publication_asset_keys(tmp_path: Path) -> None:
    """Reject output keys that would overwrite an input Asset during publication."""
    catalog, watershed, precipitation_path = make_catalog_and_watershed(tmp_path)
    run, _, _ = make_terminal_run(tmp_path, watershed, precipitation_path)
    payload = run.model_dump()
    payload["outputs"][0]["asset_key"] = "hydraulic-model"

    with pytest.raises(ValidationError, match="asset keys must be unique"):
        ScenarioRun.model_validate(payload)


def test_publisher_rejects_nonterminal_run_without_writing_manifest(tmp_path: Path) -> None:
    """Do not expose an execution whose lifecycle and provenance are incomplete."""
    catalog, watershed, precipitation_path = make_catalog_and_watershed(tmp_path)
    completed, manifest_path, _ = make_terminal_run(tmp_path, watershed, precipitation_path)
    planned = new_scenario_run(completed.spec, run_id="scenario-planned", created_at=START)

    with pytest.raises(ValueError, match="Only terminal scenario runs"):
        publish_scenario_run(catalog, planned, manifest_path, watershed)

    assert not manifest_path.exists()
