"""Versioned data contract for reproducible hydraulic scenario runs."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

CONTRACT_VERSION = "1.0.0"

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
    """Lifecycle states for one hydraulic model execution."""

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
    """Runner configuration that can affect hydraulic results."""

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


class ScenarioRunSpec(ContractModel):
    """Immutable, result-defining inputs for one hydraulic execution."""

    watershed: WatershedReference
    precipitation: PrecipitationInput
    hydraulic_model: HydraulicModel
    execution: ExecutionSpec
    antecedent_conditions: AntecedentConditions | None = None

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


class FailureDetails(ContractModel):
    """Machine-readable failure information for retry decisions."""

    error_type: Identifier
    message: NonEmptyString
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class ScenarioRun(ContractModel):
    """Versioned manifest for one planned or completed scenario run."""

    contract_version: Literal[CONTRACT_VERSION] = CONTRACT_VERSION
    run_id: Identifier
    specification_sha256: Sha256
    spec: ScenarioRunSpec
    status: RunStatus = RunStatus.PLANNED
    created_at: AwareDatetime
    started_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    outputs: list[FileReference] = Field(default_factory=list)
    quality: QualitySummary = Field(default_factory=QualitySummary)
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
            if self.failure is not None:
                raise ValueError("a succeeded run cannot contain failure details")
            if self.quality.status == QualityStatus.FAILED:
                raise ValueError("a succeeded run cannot have failed quality status")
        elif self.status == RunStatus.FAILED and self.failure is None:
            raise ValueError("a failed run must contain failure details")
        elif self.status not in {RunStatus.FAILED, RunStatus.CANCELLED} and self.failure is not None:
            raise ValueError("failure details are only valid for failed or cancelled runs")
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
