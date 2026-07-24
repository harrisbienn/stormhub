"""Tests for adapting StormHub STAC Items into scenario run specifications."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pystac
import pytest

from stormhub.scenarios import (
    ExecutionSpec,
    HydraulicStageSpec,
    HydraulicModel,
    HydrologicBoundaryMapping,
    HydrologicModel,
    HydrologicStageSpec,
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


def make_stages(
    root: Path,
    manifest_path: Path,
) -> tuple[HydrologicStageSpec, HydraulicStageSpec]:
    """Create content-addressed HMS and RAS stages with software provenance."""
    hms_path = root / "models" / "lwi-r3-hms.zip"
    ras_path = root / "models" / "lwi-r3-ras.zip"
    hms_path.parent.mkdir(parents=True)
    hms_path.write_bytes(b"versioned HEC-HMS project")
    ras_path.write_bytes(b"versioned HEC-RAS project")
    hms_package = file_reference_from_path(
        hms_path,
        manifest_path,
        asset_key="hydrologic-model",
        media_type="application/zip",
        roles=["data", "model"],
    )
    ras_package = file_reference_from_path(
        ras_path,
        manifest_path,
        asset_key="hydraulic-model",
        media_type="application/zip",
        roles=["data", "model"],
    )
    software = SoftwareProvenance(
        repository="https://example.com/flood-model-runner.git",
        revision="0123456789abcdef",
        operating_system="Windows Server 2025",
        python_version="3.12.10",
    )
    hydrologic = HydrologicStageSpec(
        model=HydrologicModel(
            model_id="lwi-r3-hms",
            model_version="2026.07",
            engine_version="4.13",
            run_name="forecast-run",
            package=hms_package,
        ),
        execution=ExecutionSpec(
            runner="windows-hec-hms-worker",
            software=software,
        ),
    )
    hydraulic = HydraulicStageSpec(
        model=HydraulicModel(
            model_id="lwi-r3-ras",
            model_version="2026.07",
            engine_version="6.6",
            plan_name="forecast-plan",
            package=ras_package,
        ),
        execution=ExecutionSpec(
            runner="windows-hec-ras-worker",
            software=software,
            parameters={"computation_interval_minutes": 5},
        ),
        boundary_mappings=[
            HydrologicBoundaryMapping(
                mapping_id="outlet-1",
                hms_element="J_OUTLET",
                hms_dss_pathname="/BASIN/J_OUTLET/FLOW//15MIN/RUN/",
                ras_boundary_id="upstream-1",
                ras_boundary_type="Flow Hydrograph",
                ras_location={"river": "River", "reach": "Reach", "station": "1000"},
                units="CFS",
                interval_minutes=15,
            )
        ],
    )
    return hydrologic, hydraulic


def test_build_scenario_spec_from_stormhub_items(tmp_path: Path) -> None:
    """Create a portable, checksum-pinned specification from existing STAC Items."""
    storm_item, watershed_item, dss_path = save_stac_inputs(tmp_path)
    manifest_path = tmp_path / "runs" / "scenario-1" / "scenario-run.json"
    hydrologic, hydraulic = make_stages(tmp_path, manifest_path)

    spec = scenario_run_spec_from_stac(
        storm_item,
        watershed_item,
        hydraulic,
        manifest_path,
        hydrologic=hydrologic,
    )

    assert spec.precipitation.duration_hours == 24
    assert spec.precipitation.rank == 1
    assert spec.precipitation.source_aorc.asset_href == "s3://noaa-nws-aorc-v1-1-1km/2020.zarr"
    assert spec.precipitation.transposed_dss.sha256 == sha256_file(dss_path)
    assert spec.precipitation.transposed_dss.asset_href == "../../catalog/24hr-events/dss/event-target.dss"
    assert spec.watershed.geometry_sha256 == geometry_sha256(watershed_item.geometry)
    assert spec.hydrologic.model.package.href == "../../models/lwi-r3-hms.zip"
    assert spec.hydraulic.model.package.href == "../../models/lwi-r3-ras.zip"


def test_adapter_rejects_changed_dss_content(tmp_path: Path) -> None:
    """Stop submission when the DSS bytes no longer match their STAC metadata."""
    storm_item, watershed_item, dss_path = save_stac_inputs(tmp_path)
    manifest_path = tmp_path / "runs" / "scenario-1" / "scenario-run.json"
    hydrologic, hydraulic = make_stages(tmp_path, manifest_path)
    dss_path.write_bytes(b"changed after catalog publication")

    with pytest.raises(ValueError, match="Checksum mismatch"):
        scenario_run_spec_from_stac(
            storm_item,
            watershed_item,
            hydraulic,
            manifest_path,
            hydrologic=hydrologic,
        )


def test_adapter_rejects_wrong_watershed(tmp_path: Path) -> None:
    """Prevent a transposed forcing file from running against another watershed."""
    storm_item, watershed_item, _ = save_stac_inputs(tmp_path)
    manifest_path = tmp_path / "runs" / "scenario-1" / "scenario-run.json"
    hydrologic, hydraulic = make_stages(tmp_path, manifest_path)
    watershed_item.id = "different-watershed"

    with pytest.raises(ValueError, match="does not match requested watershed"):
        scenario_run_spec_from_stac(
            storm_item,
            watershed_item,
            hydraulic,
            manifest_path,
            hydrologic=hydrologic,
        )
