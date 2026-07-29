"""Tests for the versioned hydrologic-hydraulic scenario run contract."""

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
    HydraulicStageSpec,
    HydraulicModel,
    HydrologicBoundaryMapping,
    HydrologicModel,
    HydrologicStageSpec,
    PrecipitationInput,
    PublicationDisposition,
    QualityStatus,
    QualitySummary,
    QualificationGate,
    QualificationStatus,
    QualificationSummary,
    RunStatus,
    ScenarioRun,
    ScenarioRunSpec,
    SoftwareProvenance,
    StageName,
    StageRun,
    StacAssetReference,
    WatershedReference,
    WorkflowKind,
    load_scenario_run,
    new_scenario_run,
    scenario_run_schema,
    write_scenario_run,
)

UTC_START = datetime(2020, 1, 2, tzinfo=timezone.utc)


def make_file_reference(
    href: str = "artifacts/model.zip",
    asset_key: str = "hydraulic-model",
) -> FileReference:
    """Create a content-addressed file reference for tests."""
    return FileReference(
        asset_key=asset_key,
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
    software = SoftwareProvenance(
        repository="https://example.com/flood-model-runner.git",
        revision="0123456789abcdef",
        operating_system="Windows Server 2025",
        python_version="3.12.10",
    )
    return ScenarioRunSpec(
        workflow=WorkflowKind.HYDROLOGIC_HYDRAULIC,
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
        hydrologic=HydrologicStageSpec(
            model=HydrologicModel(
                model_id="lwi-r3-hms",
                model_version="2026.07",
                engine_version="4.13",
                run_name="forecast-run",
                package=make_file_reference(
                    "artifacts/hms-model.zip",
                    asset_key="hydrologic-model",
                ),
            ),
            execution=ExecutionSpec(
                runner="windows-hec-hms-worker",
                software=software,
                parameters={"control_interval_minutes": 15},
            ),
        ),
        hydraulic=HydraulicStageSpec(
            model=HydraulicModel(
                model_id="lwi-r3-ras",
                model_version="2026.07",
                engine_version="6.6",
                plan_name="unsteady-plan",
                package=make_file_reference(),
            ),
            execution=ExecutionSpec(
                runner="windows-hec-ras-worker",
                software=software,
                parameters=parameters or {"computation_interval_minutes": 5},
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
        ),
    )


def rebuild_run(run: ScenarioRun, **updates) -> ScenarioRun:
    """Apply updates through validation instead of bypassing it with model_copy."""
    payload = run.model_dump()
    payload.update(updates)
    return ScenarioRun.model_validate(payload)


def make_succeeded_run() -> ScenarioRun:
    """Build a complete integrated run for qualification-policy tests."""
    run = new_scenario_run(make_spec(), created_at=UTC_START)
    completed_at = UTC_START + timedelta(minutes=30)
    return rebuild_run(
        run,
        status=RunStatus.SUCCEEDED,
        started_at=UTC_START,
        completed_at=completed_at,
        outputs=[
            make_file_reference("outputs/hms.dss", asset_key="hms-output-dss"),
            make_file_reference("outputs/wse.tif", asset_key="wse"),
        ],
        stages=[
            StageRun(
                stage=StageName.HYDROLOGIC,
                status=RunStatus.SUCCEEDED,
                started_at=UTC_START,
                completed_at=UTC_START + timedelta(minutes=10),
                output_asset_keys=["hms-output-dss"],
            ),
            StageRun(
                stage=StageName.HYDRAULIC,
                status=RunStatus.SUCCEEDED,
                started_at=UTC_START + timedelta(minutes=11),
                completed_at=completed_at,
                output_asset_keys=["wse"],
            ),
        ],
    )


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
    payload["spec"]["hydraulic"]["execution"]["parameters"]["computation_interval_minutes"] = 10

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
        outputs=[
            make_file_reference("outputs/hms.dss", asset_key="hms-output-dss"),
            make_file_reference("outputs/wse.tif", asset_key="wse"),
        ],
        stages=[
            StageRun(
                stage=StageName.HYDROLOGIC,
                status=RunStatus.SUCCEEDED,
                started_at=UTC_START,
                completed_at=UTC_START + timedelta(minutes=10),
                output_asset_keys=["hms-output-dss"],
                quality=QualitySummary(status=QualityStatus.PASSED),
            ),
            StageRun(
                stage=StageName.HYDRAULIC,
                status=RunStatus.SUCCEEDED,
                started_at=UTC_START + timedelta(minutes=11),
                completed_at=completed_at,
                output_asset_keys=["wse"],
                quality=QualitySummary(status=QualityStatus.PASSED),
            ),
        ],
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

    assert CONTRACT_VERSION in schema["properties"]["contract_version"]["enum"]
    assert {"run_id", "specification_sha256", "spec", "created_at"}.issubset(schema["required"])


def test_packaged_schema_matches_python_model() -> None:
    """Fail when a model change is committed without regenerating its schema."""
    schema_path = files("stormhub.scenarios").joinpath("schemas/scenario-run-v2.1.0.schema.json")
    packaged_schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert packaged_schema == scenario_run_schema()


def test_integrated_workflow_requires_hydrologic_stage_and_mapping() -> None:
    """Reject partial integrated specifications before an orchestrator submits them."""
    payload = make_spec().model_dump()
    payload["hydrologic"] = None

    with pytest.raises(ValidationError, match="hydrologic stage is required"):
        ScenarioRunSpec.model_validate(payload)

    payload = make_spec().model_dump()
    payload["hydraulic"]["boundary_mappings"] = []
    with pytest.raises(ValidationError, match="at least one HMS-to-RAS boundary mapping"):
        ScenarioRunSpec.model_validate(payload)


def test_succeeded_run_requires_complete_stage_provenance() -> None:
    """Do not mark an integrated run successful when the HMS stage is absent."""
    run = new_scenario_run(make_spec(), created_at=UTC_START)
    payload = run.model_dump()
    payload.update(
        {
            "status": RunStatus.SUCCEEDED,
            "started_at": UTC_START,
            "completed_at": UTC_START + timedelta(minutes=30),
            "outputs": [make_file_reference("outputs/wse.tif", asset_key="wse").model_dump()],
            "stages": [
                StageRun(
                    stage=StageName.HYDRAULIC,
                    status=RunStatus.SUCCEEDED,
                    started_at=UTC_START,
                    completed_at=UTC_START + timedelta(minutes=30),
                    output_asset_keys=["wse"],
                ).model_dump()
            ],
        }
    )

    with pytest.raises(ValidationError, match="missing stage records"):
        ScenarioRun.model_validate(payload)


def test_contract_reads_v2_manifest_with_conservative_publication_defaults() -> None:
    """Read 2.0 manifests without implicitly promoting their prior results."""
    payload = new_scenario_run(make_spec(), created_at=UTC_START).model_dump(mode="json")
    payload["contract_version"] = "2.0.0"
    payload.pop("qualification")
    payload.pop("publication")

    restored = ScenarioRun.model_validate(payload)

    assert restored.contract_version == "2.0.0"
    assert restored.publication == PublicationDisposition.PROHIBITED
    assert restored.qualification.hms_execution.status == QualificationStatus.NOT_EVALUATED


def test_forecast_promotion_requires_succeeded_run_and_all_gates_pass() -> None:
    """Keep catalog registration separate from candidate or eligible promotion."""
    run = make_succeeded_run()
    payload = run.model_dump()
    payload["publication"] = PublicationDisposition.ELIGIBLE

    with pytest.raises(ValidationError, match="all qualification gates to pass"):
        ScenarioRun.model_validate(payload)

    passed = QualificationGate(status=QualificationStatus.PASS)
    payload["qualification"] = QualificationSummary(
        hms_execution=passed,
        hydrologic_handoff=passed,
        ras_execution=passed,
        hydraulic_qaqc=passed,
    ).model_dump()
    promoted = ScenarioRun.model_validate(payload)

    assert promoted.publication == PublicationDisposition.ELIGIBLE


def test_table_metadata_requires_complete_unique_column_contract() -> None:
    """Reject partial Table extension metadata before it reaches STAC."""
    with pytest.raises(ValidationError, match="missing required fields"):
        FileReference.model_validate(
            {
                **make_file_reference().model_dump(),
                "metadata": {"table:row_count": 3},
            }
        )

    table = FileReference.model_validate(
        {
            **make_file_reference().model_dump(),
            "metadata": {
                "table:row_count": 3,
                "table:columns": [
                    {"name": "time", "type": "datetime"},
                    {"name": "flow", "type": "float64"},
                ],
            },
        }
    )
    assert table.metadata["table:row_count"] == 3
