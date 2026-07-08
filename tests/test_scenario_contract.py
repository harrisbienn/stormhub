"""Tests for the versioned hydraulic scenario run contract."""

from datetime import datetime, timedelta, timezone
from importlib.resources import files
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from stormhub.scenarios import (
    CONTRACT_VERSION,
    ExecutionSpec,
    FailureDetails,
    FileReference,
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
    load_scenario_run,
    new_scenario_run,
    scenario_run_schema,
    write_scenario_run,
)

UTC_START = datetime(2020, 1, 2, tzinfo=timezone.utc)


def make_file_reference(href: str = "artifacts/model.zip") -> FileReference:
    """Create a content-addressed file reference for tests."""
    return FileReference(
        href=href,
        media_type="application/zip",
        roles=["data", "model"],
        sha256="a" * 64,
        size_bytes=1024,
    )


def make_stac_reference(asset_key: str) -> StacAssetReference:
    """Create a portable STAC asset reference for tests."""
    return StacAssetReference(
        item_href="../../24hr-events/1/1.json",
        collection_id="24hr-events",
        item_id="1",
        asset_key=asset_key,
        asset_href=f"../../24hr-events/dss/{asset_key}.dss",
        sha256="b" * 64,
    )


def make_spec(parameters: dict | None = None) -> ScenarioRunSpec:
    """Create a complete run specification."""
    return ScenarioRunSpec(
        watershed=WatershedReference(
            watershed_id="lwi-r3-huc12-domain",
            item_href="../../hydro_domains/lwi-r3-huc12-domain.json",
            geometry_sha256="c" * 64,
        ),
        precipitation=PrecipitationInput(
            source_aorc=make_stac_reference("aorc-zarr"),
            transposed_dss=make_stac_reference("dss-target"),
            event_start=UTC_START,
            event_end=UTC_START + timedelta(hours=24),
            duration_hours=24,
            rank=1,
        ),
        hydraulic_model=HydraulicModel(
            model_id="lwi-r3-ras",
            model_version="2026.07",
            engine_version="6.6",
            plan_name="unsteady-plan",
            package=make_file_reference(),
        ),
        execution=ExecutionSpec(
            runner="windows-hec-ras-worker",
            software=SoftwareProvenance(
                repository="https://example.com/flood-model-runner.git",
                revision="0123456789abcdef",
                operating_system="Windows Server 2025",
                python_version="3.12.10",
            ),
            parameters=parameters or {"computation_interval_minutes": 5},
        ),
    )


def rebuild_run(run: ScenarioRun, **updates) -> ScenarioRun:
    """Apply updates through validation instead of bypassing it with model_copy."""
    payload = run.model_dump()
    payload.update(updates)
    return ScenarioRun.model_validate(payload)


def test_new_run_has_deterministic_identity() -> None:
    """Hash canonical JSON so mapping insertion order cannot change run identity."""
    first = new_scenario_run(make_spec({"alpha": 1, "beta": 2}), created_at=UTC_START)
    second = new_scenario_run(make_spec({"beta": 2, "alpha": 1}), created_at=UTC_START)

    assert first.contract_version == CONTRACT_VERSION
    assert first.specification_sha256 == second.specification_sha256
    assert first.run_id == second.run_id
    assert first.run_id.startswith("scenario-")


def test_manifest_round_trip_is_valid_and_atomic(tmp_path: Path) -> None:
    """Write and reload a manifest without leaving a temporary file behind."""
    run = new_scenario_run(make_spec(), created_at=UTC_START)
    path = write_scenario_run(run, tmp_path / "scenario-run.json")

    assert load_scenario_run(path) == run
    assert not (tmp_path / "scenario-run.json.tmp").exists()


def test_changed_spec_rejects_stale_digest() -> None:
    """Detect input changes that would otherwise masquerade as the same run."""
    run = new_scenario_run(make_spec(), created_at=UTC_START)
    payload = run.model_dump()
    payload["spec"]["execution"]["parameters"]["computation_interval_minutes"] = 10

    with pytest.raises(ValidationError, match="specification_sha256 does not match"):
        ScenarioRun.model_validate(payload)


def test_succeeded_run_requires_output_and_valid_timestamps() -> None:
    """Require auditable products before a run can be marked successful."""
    run = new_scenario_run(make_spec(), created_at=UTC_START)
    completed_at = UTC_START + timedelta(minutes=30)

    with pytest.raises(ValidationError, match="at least one output"):
        rebuild_run(
            run,
            status=RunStatus.SUCCEEDED,
            started_at=UTC_START,
            completed_at=completed_at,
        )

    succeeded = rebuild_run(
        run,
        status=RunStatus.SUCCEEDED,
        started_at=UTC_START,
        completed_at=completed_at,
        outputs=[make_file_reference("outputs/wse.tif")],
        quality=QualitySummary(status=QualityStatus.PASSED),
    )

    assert succeeded.status == RunStatus.SUCCEEDED


def test_failed_run_requires_failure_details() -> None:
    """Keep failed states actionable for an orchestrator or operator."""
    run = new_scenario_run(make_spec(), created_at=UTC_START)

    with pytest.raises(ValidationError, match="failure details"):
        rebuild_run(
            run,
            status=RunStatus.FAILED,
            started_at=UTC_START,
            completed_at=UTC_START + timedelta(minutes=1),
        )

    failed = rebuild_run(
        run,
        status=RunStatus.FAILED,
        started_at=UTC_START,
        completed_at=UTC_START + timedelta(minutes=1),
        failure=FailureDetails(
            error_type="ras_compute_error",
            message="HEC-RAS computation did not complete",
            retryable=True,
        ),
    )

    assert failed.failure.retryable is True


def test_contract_rejects_machine_specific_hrefs() -> None:
    """Prevent local Windows paths from leaking into portable manifests."""
    with pytest.raises(ValidationError, match="portable"):
        make_file_reference(r"C:\model\project.zip")


def test_transposed_dss_requires_checksum() -> None:
    """Content-address the generated forcing file used by the hydraulic model."""
    target = make_stac_reference("dss-target").model_copy(update={"sha256": None})

    with pytest.raises(ValidationError, match="transposed_dss.*SHA-256"):
        PrecipitationInput(
            source_aorc=make_stac_reference("AORC"),
            transposed_dss=target,
            event_start=UTC_START,
            event_end=UTC_START + timedelta(hours=24),
            duration_hours=24,
        )


def test_schema_exposes_version_and_required_run_fields() -> None:
    """Expose a JSON Schema for non-Python producers and consumers."""
    schema = scenario_run_schema()

    assert schema["properties"]["contract_version"]["const"] == CONTRACT_VERSION
    assert {"run_id", "specification_sha256", "spec", "created_at"}.issubset(schema["required"])


def test_packaged_schema_matches_python_model() -> None:
    """Fail when a model change is committed without regenerating its schema."""
    schema_path = files("stormhub.scenarios").joinpath("schemas/scenario-run-v1.0.0.schema.json")
    packaged_schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert packaged_schema == scenario_run_schema()
