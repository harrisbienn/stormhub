"""Tests for adapting StormHub STAC Items into scenario run specifications."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pystac
import pytest

from stormhub.scenarios import (
    ExecutionSpec,
    HydraulicModel,
    SoftwareProvenance,
    file_reference_from_path,
    geometry_sha256,
    scenario_run_spec_from_stac,
)
from stormhub.utils import sha256_file, sha256_multihash


def save_stac_inputs(root: Path) -> tuple[pystac.Item, pystac.Item, Path]:
    """Create a storm Item, target DSS, and watershed Item on disk."""
    item_dir = root / "catalog" / "24hr-events" / "1"
    dss_dir = root / "catalog" / "24hr-events" / "dss"
    watershed_dir = root / "catalog" / "hydro_domains"
    item_dir.mkdir(parents=True)
    dss_dir.mkdir(parents=True)
    watershed_dir.mkdir(parents=True)

    dss_path = dss_dir / "event-target.dss"
    dss_path.write_bytes(b"transposed precipitation")
    checksum = sha256_multihash(sha256_file(dss_path))

    start = datetime(2020, 1, 2, tzinfo=timezone.utc)
    storm_item = pystac.Item(
        id="1",
        geometry={"type": "Point", "coordinates": [-92.5, 32.5]},
        bbox=[-92.5, 32.5, -92.5, 32.5],
        datetime=None,
        properties={"aorc:collection_rank": 1},
        start_datetime=start,
        end_datetime=start + timedelta(hours=24),
        collection="24hr-events",
    )
    storm_item.add_asset(
        "AORC",
        pystac.Asset(
            href="s3://noaa-nws-aorc-v1-1-1km/2020.zarr",
            media_type=pystac.MediaType.ZARR,
            roles=["data"],
        ),
    )
    storm_item.add_asset(
        "dss-target",
        pystac.Asset(
            href="../dss/event-target.dss",
            media_type="application/x-dss",
            roles=["data", "target"],
            extra_fields={
                "file:checksum": checksum,
                "file:size": dss_path.stat().st_size,
                "stormhub:spatial_role": "target",
                "stormhub:target_watershed_id": "lwi-r3-huc12-domain",
                "stormhub:validation_status": "passed",
            },
        ),
    )
    storm_item.set_self_href(str(item_dir / "1.json"))
    storm_item.save_object(include_self_link=False)

    watershed_geometry = {
        "type": "Polygon",
        "coordinates": [[[-93, 32], [-92, 32], [-92, 33], [-93, 33], [-93, 32]]],
    }
    watershed_item = pystac.Item(
        id="lwi-r3-huc12-domain",
        geometry=watershed_geometry,
        bbox=[-93, 32, -92, 33],
        datetime=start,
        properties={"hydro_domain:type": "watershed"},
    )
    watershed_item.set_self_href(str(watershed_dir / "lwi-r3-huc12-domain.json"))
    watershed_item.save_object(include_self_link=False)
    return storm_item, watershed_item, dss_path


def make_model_and_execution(root: Path, manifest_path: Path) -> tuple[HydraulicModel, ExecutionSpec]:
    """Create content-addressed hydraulic model and software provenance."""
    model_path = root / "models" / "lwi-r3-ras.zip"
    model_path.parent.mkdir(parents=True)
    model_path.write_bytes(b"versioned HEC-RAS project")
    model_package = file_reference_from_path(
        model_path,
        manifest_path,
        asset_key="hydraulic-model",
        media_type="application/zip",
        roles=["data", "model"],
    )
    model = HydraulicModel(
        model_id="lwi-r3-ras",
        model_version="2026.07",
        engine_version="6.6",
        plan_name="forecast-plan",
        package=model_package,
    )
    execution = ExecutionSpec(
        runner="windows-hec-ras-worker",
        software=SoftwareProvenance(
            repository="https://example.com/flood-model-runner.git",
            revision="0123456789abcdef",
            operating_system="Windows Server 2025",
            python_version="3.12.10",
        ),
        parameters={"computation_interval_minutes": 5},
    )
    return model, execution


def test_build_scenario_spec_from_stormhub_items(tmp_path: Path) -> None:
    """Create a portable, checksum-pinned specification from existing STAC Items."""
    storm_item, watershed_item, dss_path = save_stac_inputs(tmp_path)
    manifest_path = tmp_path / "runs" / "scenario-1" / "scenario-run.json"
    model, execution = make_model_and_execution(tmp_path, manifest_path)

    spec = scenario_run_spec_from_stac(
        storm_item,
        watershed_item,
        model,
        execution,
        manifest_path,
    )

    assert spec.precipitation.duration_hours == 24
    assert spec.precipitation.rank == 1
    assert spec.precipitation.source_aorc.asset_href == "s3://noaa-nws-aorc-v1-1-1km/2020.zarr"
    assert spec.precipitation.transposed_dss.sha256 == sha256_file(dss_path)
    assert spec.precipitation.transposed_dss.asset_href == "../../catalog/24hr-events/dss/event-target.dss"
    assert spec.watershed.geometry_sha256 == geometry_sha256(watershed_item.geometry)
    assert spec.hydraulic_model.package.href == "../../models/lwi-r3-ras.zip"


def test_adapter_rejects_changed_dss_content(tmp_path: Path) -> None:
    """Stop submission when the DSS bytes no longer match their STAC metadata."""
    storm_item, watershed_item, dss_path = save_stac_inputs(tmp_path)
    manifest_path = tmp_path / "runs" / "scenario-1" / "scenario-run.json"
    model, execution = make_model_and_execution(tmp_path, manifest_path)
    dss_path.write_bytes(b"changed after catalog publication")

    with pytest.raises(ValueError, match="Checksum mismatch"):
        scenario_run_spec_from_stac(
            storm_item,
            watershed_item,
            model,
            execution,
            manifest_path,
        )


def test_adapter_rejects_wrong_watershed(tmp_path: Path) -> None:
    """Prevent a transposed forcing file from running against another watershed."""
    storm_item, watershed_item, _ = save_stac_inputs(tmp_path)
    manifest_path = tmp_path / "runs" / "scenario-1" / "scenario-run.json"
    model, execution = make_model_and_execution(tmp_path, manifest_path)
    watershed_item.id = "different-watershed"

    with pytest.raises(ValueError, match="does not match requested watershed"):
        scenario_run_spec_from_stac(
            storm_item,
            watershed_item,
            model,
            execution,
            manifest_path,
        )
