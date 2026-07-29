"""Versioned data contract for reproducible hydrologic-hydraulic scenario runs."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

CONTRACT_VERSION = "2.1.0"
COMPATIBLE_CONTRACT_VERSIONS = ("2.0.0", CONTRACT_VERSION)

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Identifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"),
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


class ContractModel(BaseModel):
    """Base model that rejects misspelled or undocumented fields."""

    model_config = ConfigDict(extra="forbid", frozen=True, protected_namespaces=())


class RunStatus(str, Enum):
    """Lifecycle states for an integrated run or one model stage."""

    PLANNED = "planned"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class QualityStatus(str, Enum):
    """Aggregate or individual quality-control result."""

    NOT_RUN = "not_run"
    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"


class QualificationStatus(str, Enum):
    """Decision state for one integrated qualification gate."""

    NOT_EVALUATED = "not_evaluated"
    PASS = "pass"
    CONDITIONAL = "conditional"
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"


class PublicationDisposition(str, Enum):
    """Approved use of a cataloged scenario response."""

    PROHIBITED = "prohibited"
    INTERNAL = "internal"
    CANDIDATE = "candidate"
    ELIGIBLE = "eligible"
    RETIRED = "retired"


class WorkflowKind(str, Enum):
    """Supported model-stage topologies."""

    HYDROLOGIC_HYDRAULIC = "hydrologic_hydraulic"
    HYDRAULIC_ONLY = "hydraulic_only"


class StageName(str, Enum):
    """Stable names for model stages recorded in run provenance."""

    HYDROLOGIC = "hydrologic"
    HYDRAULIC = "hydraulic"


def _validate_href(value: str | None) -> str | None:
    """Require a portable relative href or an IRI-like absolute href."""
    if value is not None and ("\\" in value or re.match(r"^[A-Za-z]:[/\\]", value)):
        raise ValueError("hrefs must be portable and use forward slashes, not Windows filesystem paths")
    return value


def _validate_utc(value: datetime | None) -> datetime | None:
    """Require UTC timestamps so workers cannot disagree about clock offsets."""
    if value is not None and value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError("timestamps must use UTC")
    return value


class StacAssetReference(ContractModel):
    """Stable reference to one asset on one STAC Item."""

    kind: Literal["stac_asset"] = "stac_asset"
    item_href: NonEmptyString
    collection_id: Identifier
    item_id: Identifier
    asset_key: Identifier
    asset_href: NonEmptyString | None = None
    sha256: Sha256 | None = None

    _portable_item_href = field_validator("item_href")(_validate_href)
    _portable_asset_href = field_validator("asset_href")(_validate_href)


class FileReference(ContractModel):
    """Content-addressed reference to a run input or output file."""

    kind: Literal["file"] = "file"
    asset_key: Identifier
    href: NonEmptyString
    media_type: NonEmptyString
    roles: list[NonEmptyString] = Field(min_length=1)
    sha256: Sha256
    size_bytes: int = Field(ge=0)
    title: NonEmptyString | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    _portable_href = field_validator("href")(_validate_href)

    @field_validator("roles")
    @classmethod
    def roles_are_unique(cls, value: list[str]) -> list[str]:
        """Reject duplicate roles because they usually indicate producer bugs."""
        if len(value) != len(set(value)):
            raise ValueError("roles must be unique")
        return value

    @field_validator("metadata")
    @classmethod
    def table_metadata_is_complete(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Validate Table extension fields before publication declares it."""
        table_keys = {key for key in value if key.startswith("table:")}
        if not table_keys:
            return value

        required = {"table:columns", "table:row_count"}
        missing = required - set(value)
        if missing:
            raise ValueError(f"table metadata is missing required fields: {sorted(missing)}")

        row_count = value["table:row_count"]
        if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count < 0:
            raise ValueError("table:row_count must be a non-negative integer")

        columns = value["table:columns"]
        if not isinstance(columns, list) or not columns:
            raise ValueError("table:columns must be a non-empty list")
        names: list[str] = []
        for column in columns:
            if not isinstance(column, dict):
                raise ValueError("each table:columns entry must be an object")
            name = column.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("each table:columns entry must contain a non-empty name")
            names.append(name)
        if len(names) != len(set(names)):
            raise ValueError("table:columns names must be unique")
        return value


class WatershedReference(ContractModel):
    """Target watershed used by both precipitation and hydraulic processing."""

    watershed_id: Identifier
    item_href: NonEmptyString
    geometry_sha256: Sha256

    _portable_item_href = field_validator("item_href")(_validate_href)


class PrecipitationInput(ContractModel):
    """Source and watershed-transposed precipitation for the run."""

    source_aorc: StacAssetReference
    transposed_dss: StacAssetReference
    event_start: AwareDatetime
    event_end: AwareDatetime
    duration_hours: int = Field(gt=0)
    rank: int | None = Field(default=None, gt=0)

    _utc_start = field_validator("event_start")(_validate_utc)
    _utc_end = field_validator("event_end")(_validate_utc)

    @model_validator(mode="after")
    def dates_match_duration(self) -> PrecipitationInput:
        """Keep the declared duration consistent with the event timestamps."""
        actual_hours = (self.event_end - self.event_start).total_seconds() / 3600
        if actual_hours <= 0:
            raise ValueError("event_end must be after event_start")
        if actual_hours != self.duration_hours:
            raise ValueError("duration_hours must equal the event timestamp interval")
        if self.transposed_dss.sha256 is None:
            raise ValueError("transposed_dss must include a SHA-256 checksum")
        return self


class HydraulicModel(ContractModel):
    """Versioned hydraulic model package and selected plan."""

    model_id: Identifier
    model_version: NonEmptyString
    engine: NonEmptyString = "HEC-RAS"
    engine_version: NonEmptyString
    plan_name: NonEmptyString
    package: FileReference


class HydrologicModel(ContractModel):
    """Versioned hydrologic model package and selected HMS run."""

    model_id: Identifier
    model_version: NonEmptyString
    engine: NonEmptyString = "HEC-HMS"
    engine_version: NonEmptyString
    run_name: NonEmptyString
    package: FileReference


class SoftwareProvenance(ContractModel):
    """Code and runtime identity used to execute a scenario."""

    repository: NonEmptyString
    revision: NonEmptyString
    dirty: bool = False
    container_image: NonEmptyString | None = None
    container_digest: NonEmptyString | None = None
    python_version: NonEmptyString | None = None
    operating_system: NonEmptyString | None = None

    @model_validator(mode="after")
    def digest_requires_image(self) -> SoftwareProvenance:
        """Prevent an unresolvable container digest."""
        if self.container_digest and not self.container_image:
            raise ValueError("container_image is required when container_digest is provided")
        return self


class ExecutionSpec(ContractModel):
    """Runner configuration that can affect a model stage's results."""

    runner: NonEmptyString
    software: SoftwareProvenance
    command: list[NonEmptyString] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)


class AntecedentConditions(ContractModel):
    """Optional hydrologic state applied before the precipitation event."""

    condition_id: Identifier
    as_of: AwareDatetime
    parameters: dict[str, Any] = Field(default_factory=dict)
    sources: list[StacAssetReference] = Field(default_factory=list)

    _utc_as_of = field_validator("as_of")(_validate_utc)


class HydrologicBoundaryMapping(ContractModel):
    """Explicit link from one HMS result pathname to one RAS boundary."""

    mapping_id: Identifier
    hms_element: NonEmptyString
    hms_parameter: NonEmptyString = "FLOW"
    hms_dss_pathname: NonEmptyString
    ras_boundary_id: Identifier
    ras_boundary_type: NonEmptyString
    ras_location: dict[str, NonEmptyString] = Field(min_length=1)
    units: NonEmptyString
    interval_minutes: int = Field(gt=0)


class HydrologicStageSpec(ContractModel):
    """Result-defining HMS model and execution configuration."""

    model: HydrologicModel
    execution: ExecutionSpec


class HydraulicStageSpec(ContractModel):
    """Result-defining RAS model, execution, and upstream mappings."""

    model: HydraulicModel
    execution: ExecutionSpec
    boundary_mappings: list[HydrologicBoundaryMapping] = Field(default_factory=list)

    @field_validator("boundary_mappings")
    @classmethod
    def mapping_ids_are_unique(
        cls,
        value: list[HydrologicBoundaryMapping],
    ) -> list[HydrologicBoundaryMapping]:
        """Reject mappings that could overwrite one another during handoff."""
        mapping_ids = [mapping.mapping_id for mapping in value]
        if len(mapping_ids) != len(set(mapping_ids)):
            raise ValueError("boundary mapping IDs must be unique")
        boundary_ids = [mapping.ras_boundary_id for mapping in value]
        if len(boundary_ids) != len(set(boundary_ids)):
            raise ValueError("RAS boundary IDs must be unique")
        return value


class ScenarioRunSpec(ContractModel):
    """Immutable inputs for one hydraulic-only or HMS-to-RAS execution."""

    workflow: WorkflowKind
    watershed: WatershedReference
    precipitation: PrecipitationInput
    hydrologic: HydrologicStageSpec | None = None
    hydraulic: HydraulicStageSpec
    antecedent_conditions: AntecedentConditions | None = None

    @model_validator(mode="after")
    def stages_match_workflow(self) -> ScenarioRunSpec:
        """Require an explicit, internally consistent stage topology."""
        if self.workflow == WorkflowKind.HYDROLOGIC_HYDRAULIC:
            if self.hydrologic is None:
                raise ValueError("hydrologic stage is required for hydrologic_hydraulic workflow")
            if not self.hydraulic.boundary_mappings:
                raise ValueError("at least one HMS-to-RAS boundary mapping is required")
        elif self.hydrologic is not None:
            raise ValueError("hydrologic stage is not valid for hydraulic_only workflow")
        return self

    def sha256(self) -> str:
        """Return a deterministic digest of all result-defining inputs."""
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class QualityCheck(ContractModel):
    """One named validation performed on inputs, execution, or outputs."""

    name: Identifier
    status: QualityStatus
    message: NonEmptyString | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)


class QualitySummary(ContractModel):
    """Aggregate quality state and its supporting checks."""

    status: QualityStatus = QualityStatus.NOT_RUN
    checks: list[QualityCheck] = Field(default_factory=list)


class QualificationGate(ContractModel):
    """One independently reviewable integrated qualification decision."""

    status: QualificationStatus = QualificationStatus.NOT_EVALUATED
    evidence_asset_keys: list[Identifier] = Field(default_factory=list)
    message: NonEmptyString | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)

    @field_validator("evidence_asset_keys")
    @classmethod
    def evidence_asset_keys_are_unique(cls, value: list[str]) -> list[str]:
        """Reject ambiguous duplicate evidence references."""
        if len(value) != len(set(value)):
            raise ValueError("qualification evidence asset keys must be unique")
        return value


class QualificationSummary(ContractModel):
    """Four gates that separate execution completion from model qualification."""

    hms_execution: QualificationGate = Field(default_factory=QualificationGate)
    hydrologic_handoff: QualificationGate = Field(default_factory=QualificationGate)
    ras_execution: QualificationGate = Field(default_factory=QualificationGate)
    hydraulic_qaqc: QualificationGate = Field(default_factory=QualificationGate)

    def required_gates(self, workflow: WorkflowKind) -> tuple[QualificationGate, ...]:
        """Return gates that must pass before this workflow can be promoted."""
        hydraulic = (self.ras_execution, self.hydraulic_qaqc)
        if workflow == WorkflowKind.HYDROLOGIC_HYDRAULIC:
            return (self.hms_execution, self.hydrologic_handoff, *hydraulic)
        return hydraulic


class FailureDetails(ContractModel):
    """Machine-readable failure information for retry decisions."""

    error_type: Identifier
    message: NonEmptyString
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class StageRun(ContractModel):
    """Lifecycle and output references for one model stage."""

    stage: StageName
    status: RunStatus
    started_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    output_asset_keys: list[Identifier] = Field(default_factory=list)
    quality: QualitySummary = Field(default_factory=QualitySummary)
    failure: FailureDetails | None = None

    _utc_started = field_validator("started_at")(_validate_utc)
    _utc_completed = field_validator("completed_at")(_validate_utc)

    @model_validator(mode="after")
    def validate_lifecycle(self) -> StageRun:
        """Require sufficient provenance for the declared stage state."""
        terminal = {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}
        started = terminal | {RunStatus.RUNNING}
        if self.status in started and self.started_at is None:
            raise ValueError(f"started_at is required for stage status '{self.status.value}'")
        if self.status in terminal and self.completed_at is None:
            raise ValueError(f"completed_at is required for stage status '{self.status.value}'")
        if self.completed_at and self.started_at and self.completed_at < self.started_at:
            raise ValueError("stage completed_at cannot precede started_at")
        if len(self.output_asset_keys) != len(set(self.output_asset_keys)):
            raise ValueError("stage output asset keys must be unique")

        if self.status == RunStatus.SUCCEEDED:
            if not self.output_asset_keys:
                raise ValueError("a succeeded stage must reference at least one output asset")
            if self.failure is not None:
                raise ValueError("a succeeded stage cannot contain failure details")
            if self.quality.status == QualityStatus.FAILED:
                raise ValueError("a succeeded stage cannot have failed quality status")
        elif self.status == RunStatus.FAILED and self.failure is None:
            raise ValueError("a failed stage must contain failure details")
        elif self.status not in {RunStatus.FAILED, RunStatus.CANCELLED} and self.failure is not None:
            raise ValueError("stage failure details are only valid for failed or cancelled stages")
        return self


class ScenarioRun(ContractModel):
    """Versioned manifest for one planned or completed scenario run."""

    contract_version: Literal["2.0.0", "2.1.0"] = CONTRACT_VERSION
    run_id: Identifier
    specification_sha256: Sha256
    spec: ScenarioRunSpec
    status: RunStatus = RunStatus.PLANNED
    created_at: AwareDatetime
    started_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    stages: list[StageRun] = Field(default_factory=list)
    outputs: list[FileReference] = Field(default_factory=list)
    quality: QualitySummary = Field(default_factory=QualitySummary)
    qualification: QualificationSummary = Field(default_factory=QualificationSummary)
    publication: PublicationDisposition = PublicationDisposition.PROHIBITED
    failure: FailureDetails | None = None
    labels: dict[str, str] = Field(default_factory=dict)

    _utc_created = field_validator("created_at")(_validate_utc)
    _utc_started = field_validator("started_at")(_validate_utc)
    _utc_completed = field_validator("completed_at")(_validate_utc)

    @model_validator(mode="after")
    def validate_identity_and_lifecycle(self) -> ScenarioRun:
        """Validate digest integrity and state-dependent fields."""
        if self.specification_sha256 != self.spec.sha256():
            raise ValueError("specification_sha256 does not match spec")

        asset_keys = [
            self.spec.precipitation.transposed_dss.asset_key,
            self.spec.hydraulic.model.package.asset_key,
            *(output.asset_key for output in self.outputs),
        ]
        if self.spec.hydrologic is not None:
            asset_keys.append(self.spec.hydrologic.model.package.asset_key)
        if "scenario-run" in asset_keys:
            raise ValueError("asset key 'scenario-run' is reserved for the contract manifest")
        if len(asset_keys) != len(set(asset_keys)):
            raise ValueError("DSS, model package, and output asset keys must be unique")
        published_asset_keys = {
            "scenario-run",
            "target-precipitation-dss",
            self.spec.hydraulic.model.package.asset_key,
            *(output.asset_key for output in self.outputs),
        }
        if self.spec.hydrologic is not None:
            published_asset_keys.add(self.spec.hydrologic.model.package.asset_key)
        if len(published_asset_keys) != 3 + len(self.outputs) + int(self.spec.hydrologic is not None):
            raise ValueError("scenario publication asset keys must be unique")

        stage_names = [stage.stage for stage in self.stages]
        if len(stage_names) != len(set(stage_names)):
            raise ValueError("model stages must be unique")
        output_asset_keys = {output.asset_key for output in self.outputs}
        for stage in self.stages:
            unknown = set(stage.output_asset_keys) - output_asset_keys
            if unknown:
                raise ValueError(
                    f"stage '{stage.stage.value}' references unknown output assets: {sorted(unknown)}"
                )
        evidence_asset_keys = {
            "scenario-run",
            "target-precipitation-dss",
            self.spec.hydraulic.model.package.asset_key,
            *(output.asset_key for output in self.outputs),
        }
        if self.spec.hydrologic is not None:
            evidence_asset_keys.add(self.spec.hydrologic.model.package.asset_key)
        for gate_name, gate in self.qualification:
            unknown = set(gate.evidence_asset_keys) - evidence_asset_keys
            if unknown:
                raise ValueError(
                    f"qualification gate '{gate_name}' references unknown evidence assets: {sorted(unknown)}"
                )

        terminal = {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}
        started = terminal | {RunStatus.RUNNING}
        if self.status in started and self.started_at is None:
            raise ValueError(f"started_at is required for status '{self.status.value}'")
        if self.status in terminal and self.completed_at is None:
            raise ValueError(f"completed_at is required for status '{self.status.value}'")
        if self.started_at and self.started_at < self.created_at:
            raise ValueError("started_at cannot precede created_at")
        if self.completed_at and self.started_at and self.completed_at < self.started_at:
            raise ValueError("completed_at cannot precede started_at")

        if self.status == RunStatus.SUCCEEDED:
            if not self.outputs:
                raise ValueError("a succeeded run must contain at least one output")
            required_stages = {StageName.HYDRAULIC}
            if self.spec.workflow == WorkflowKind.HYDROLOGIC_HYDRAULIC:
                required_stages.add(StageName.HYDROLOGIC)
            stages_by_name = {stage.stage: stage for stage in self.stages}
            missing_stages = required_stages - set(stages_by_name)
            if missing_stages:
                raise ValueError(
                    f"a succeeded run is missing stage records: "
                    f"{sorted(stage.value for stage in missing_stages)}"
                )
            incomplete_stages = [
                stage.value
                for stage in required_stages
                if stages_by_name[stage].status != RunStatus.SUCCEEDED
            ]
            if incomplete_stages:
                raise ValueError(
                    f"a succeeded run requires succeeded stages: {sorted(incomplete_stages)}"
                )
            if self.failure is not None:
                raise ValueError("a succeeded run cannot contain failure details")
            if self.quality.status == QualityStatus.FAILED:
                raise ValueError("a succeeded run cannot have failed quality status")
        elif self.status == RunStatus.FAILED and self.failure is None:
            raise ValueError("a failed run must contain failure details")
        elif self.status not in {RunStatus.FAILED, RunStatus.CANCELLED} and self.failure is not None:
            raise ValueError("failure details are only valid for failed or cancelled runs")

        if self.status in {RunStatus.FAILED, RunStatus.CANCELLED}:
            if self.publication != PublicationDisposition.PROHIBITED:
                raise ValueError("failed or cancelled runs must have prohibited publication")

        promotion_states = {
            PublicationDisposition.CANDIDATE,
            PublicationDisposition.ELIGIBLE,
        }
        if self.publication in promotion_states:
            if self.status != RunStatus.SUCCEEDED:
                raise ValueError(
                    f"publication disposition '{self.publication.value}' requires a succeeded run"
                )
            incomplete = [
                gate.status.value
                for gate in self.qualification.required_gates(self.spec.workflow)
                if gate.status != QualificationStatus.PASS
            ]
            if incomplete:
                raise ValueError(
                    f"publication disposition '{self.publication.value}' requires all qualification gates to pass"
                )
        return self


def new_scenario_run(
    spec: ScenarioRunSpec,
    *,
    run_id: str | None = None,
    created_at: datetime | None = None,
    labels: dict[str, str] | None = None,
) -> ScenarioRun:
    """Create a planned run with deterministic identity from its specification."""
    digest = spec.sha256()
    return ScenarioRun(
        run_id=run_id or f"scenario-{digest[:16]}",
        specification_sha256=digest,
        spec=spec,
        created_at=created_at or datetime.now(timezone.utc),
        labels=labels or {},
    )


def load_scenario_run(path: str | Path) -> ScenarioRun:
    """Load and validate a scenario run manifest from JSON."""
    return ScenarioRun.model_validate_json(Path(path).read_text(encoding="utf-8"))


def write_scenario_run(run: ScenarioRun, path: str | Path) -> Path:
    """Atomically write a scenario run manifest as formatted JSON."""
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(f"{output_path.suffix}.tmp")
    temporary_path.write_text(run.model_dump_json(indent=2) + "\n", encoding="utf-8")
    temporary_path.replace(output_path)
    return output_path


def scenario_run_schema() -> dict[str, Any]:
    """Return the JSON Schema for the current scenario run contract."""
    return ScenarioRun.model_json_schema(
        ref_template="#/$defs/{model}",
        mode="serialization",
    )


def write_scenario_run_schema(path: str | Path) -> Path:
    """Atomically write the current contract's JSON Schema."""
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(f"{output_path.suffix}.tmp")
    payload = json.dumps(scenario_run_schema(), indent=2, sort_keys=True) + "\n"
    temporary_path.write_text(payload, encoding="utf-8")
    temporary_path.replace(output_path)
    return output_path
