"""Immutable qualification assessments and publication decisions for scenario responses."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from stormhub.scenarios.contract import (
    ContractModel,
    FileReference,
    Identifier,
    NonEmptyString,
    PublicationDisposition,
    QualificationStatus,
    ScenarioResponseIdentity,
    Sha256,
    _validate_utc,
)

ASSESSMENT_CONTRACT_VERSION = "1.0.0"
PROMOTION_CONTRACT_VERSION = "1.0.0"
ASSESSMENT_ASSET_KEY = "qualification-assessment"


def _canonical_sha256(payload: dict[str, Any]) -> str:
    """Hash a model payload with deterministic JSON encoding."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _record_identity(prefix: str, payload: dict[str, Any]) -> str:
    """Create a compact content-derived identifier for one immutable record."""
    return f"{prefix}-{_canonical_sha256(payload)[:24]}"


def _json_datetime(value: datetime) -> str:
    """Match Pydantic's UTC JSON timestamp representation."""
    return value.isoformat().replace("+00:00", "Z")


def _record_bytes(record: ContractModel) -> bytes:
    """Serialize one contract record in its stable on-disk representation."""
    return (record.model_dump_json(indent=2) + "\n").encode("utf-8")


def _write_append_only(record: ContractModel, path: str | Path) -> Path:
    """Write a record once, allowing only byte-identical idempotent retries."""
    output_path = Path(path)
    payload = _record_bytes(record)
    if output_path.exists():
        if output_path.read_bytes() == payload:
            return output_path
        raise ValueError(f"Append-only response record already exists with different content: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(f"{output_path.suffix}.tmp")
    temporary_path.write_bytes(payload)
    temporary_path.replace(output_path)
    return output_path


class ScenarioAssessment(ContractModel):
    """Policy-pinned qualification of one immutable scenario response run."""

    contract_version: Literal["1.0.0"] = ASSESSMENT_CONTRACT_VERSION
    assessment_id: Identifier
    run_id: Identifier
    specification_sha256: Sha256
    response: ScenarioResponseIdentity
    assessment: FileReference
    status: QualificationStatus
    recommended_publication: PublicationDisposition = PublicationDisposition.PROHIBITED
    created_at: AwareDatetime

    _utc_created = field_validator("created_at")(_validate_utc)

    @model_validator(mode="after")
    def validate_identity_and_recommendation(self) -> ScenarioAssessment:
        """Authenticate the record and prevent policy evaluation from granting eligibility."""
        if self.assessment.asset_key != ASSESSMENT_ASSET_KEY:
            raise ValueError(f"assessment asset_key must be '{ASSESSMENT_ASSET_KEY}'")
        if self.recommended_publication == PublicationDisposition.ELIGIBLE:
            raise ValueError("an assessment may recommend candidate status but cannot grant eligibility")
        if (
            self.recommended_publication == PublicationDisposition.CANDIDATE
            and self.status != QualificationStatus.PASS
        ):
            raise ValueError("candidate recommendation requires a passing assessment")
        expected = _record_identity(
            "assessment",
            self.model_dump(mode="json", exclude={"assessment_id"}),
        )
        if self.assessment_id != expected:
            raise ValueError("assessment_id does not match assessment content")
        return self

    def sha256(self) -> str:
        """Return the canonical content digest used by promotion references."""
        return _canonical_sha256(self.model_dump(mode="json"))


class ScenarioPromotion(ContractModel):
    """Append-only operator decision controlling forecast-response eligibility."""

    contract_version: Literal["1.0.0"] = PROMOTION_CONTRACT_VERSION
    promotion_id: Identifier
    run_id: Identifier
    specification_sha256: Sha256
    response: ScenarioResponseIdentity
    assessment_id: Identifier
    assessment_sha256: Sha256
    assessment_status: QualificationStatus
    disposition: PublicationDisposition
    decided_at: AwareDatetime
    authority: NonEmptyString
    rationale: NonEmptyString
    supersedes_promotion_id: Identifier | None = None
    evidence: list[FileReference] = Field(default_factory=list)

    _utc_decided = field_validator("decided_at")(_validate_utc)

    @field_validator("evidence")
    @classmethod
    def evidence_keys_are_unique(cls, value: list[FileReference]) -> list[FileReference]:
        """Reject duplicate evidence assets that would collide in STAC."""
        keys = [reference.asset_key for reference in value]
        if len(keys) != len(set(keys)):
            raise ValueError("promotion evidence asset keys must be unique")
        return value

    @model_validator(mode="after")
    def validate_identity_and_disposition(self) -> ScenarioPromotion:
        """Authenticate the record and require a pass before positive promotion."""
        if (
            self.disposition
            in {PublicationDisposition.CANDIDATE, PublicationDisposition.ELIGIBLE}
            and self.assessment_status != QualificationStatus.PASS
        ):
            raise ValueError(
                f"publication disposition '{self.disposition.value}' requires a passing assessment"
            )
        expected = _record_identity(
            "promotion",
            self.model_dump(mode="json", exclude={"promotion_id"}),
        )
        if self.promotion_id != expected:
            raise ValueError("promotion_id does not match promotion content")
        return self

    def sha256(self) -> str:
        """Return the canonical digest of this immutable decision."""
        return _canonical_sha256(self.model_dump(mode="json"))


def new_scenario_assessment(
    *,
    run_id: str,
    specification_sha256: str,
    response: ScenarioResponseIdentity,
    assessment: FileReference,
    status: QualificationStatus,
    recommended_publication: PublicationDisposition = PublicationDisposition.PROHIBITED,
    created_at: datetime | None = None,
) -> ScenarioAssessment:
    """Create a content-addressed assessment record."""
    timestamp = created_at or datetime.now(timezone.utc)
    payload = {
        "contract_version": ASSESSMENT_CONTRACT_VERSION,
        "run_id": run_id,
        "specification_sha256": specification_sha256,
        "response": response.model_dump(mode="json"),
        "assessment": assessment.model_dump(mode="json"),
        "status": status.value,
        "recommended_publication": recommended_publication.value,
        "created_at": _json_datetime(timestamp),
    }
    assessment_id = _record_identity("assessment", payload)
    return ScenarioAssessment(assessment_id=assessment_id, **payload)


def new_scenario_promotion(
    *,
    assessment: ScenarioAssessment,
    disposition: PublicationDisposition,
    authority: str,
    rationale: str,
    decided_at: datetime | None = None,
    supersedes_promotion_id: str | None = None,
    evidence: list[FileReference] | None = None,
) -> ScenarioPromotion:
    """Create a content-addressed publication decision from an assessment."""
    timestamp = decided_at or datetime.now(timezone.utc)
    payload = {
        "contract_version": PROMOTION_CONTRACT_VERSION,
        "run_id": assessment.run_id,
        "specification_sha256": assessment.specification_sha256,
        "response": assessment.response.model_dump(mode="json"),
        "assessment_id": assessment.assessment_id,
        "assessment_sha256": assessment.sha256(),
        "assessment_status": assessment.status.value,
        "disposition": disposition.value,
        "decided_at": _json_datetime(timestamp),
        "authority": authority,
        "rationale": rationale,
        "supersedes_promotion_id": supersedes_promotion_id,
        "evidence": [
            reference.model_dump(mode="json") for reference in (evidence or [])
        ],
    }
    promotion_id = _record_identity("promotion", payload)
    return ScenarioPromotion(promotion_id=promotion_id, **payload)


def load_scenario_assessment(path: str | Path) -> ScenarioAssessment:
    """Load and authenticate an assessment record."""
    return ScenarioAssessment.model_validate_json(Path(path).read_text(encoding="utf-8"))


def load_scenario_promotion(path: str | Path) -> ScenarioPromotion:
    """Load and authenticate a promotion record."""
    return ScenarioPromotion.model_validate_json(Path(path).read_text(encoding="utf-8"))


def write_scenario_assessment(record: ScenarioAssessment, path: str | Path) -> Path:
    """Write an assessment with append-only retry semantics."""
    return _write_append_only(record, path)


def write_scenario_promotion(record: ScenarioPromotion, path: str | Path) -> Path:
    """Write a promotion with append-only retry semantics."""
    return _write_append_only(record, path)


def scenario_assessment_schema() -> dict[str, Any]:
    """Return the JSON Schema for assessment records."""
    return ScenarioAssessment.model_json_schema(ref_template="#/$defs/{model}", mode="serialization")


def scenario_promotion_schema() -> dict[str, Any]:
    """Return the JSON Schema for promotion records."""
    return ScenarioPromotion.model_json_schema(ref_template="#/$defs/{model}", mode="serialization")


def _write_schema(payload: dict[str, Any], path: str | Path) -> Path:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(f"{output_path.suffix}.tmp")
    temporary_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary_path.replace(output_path)
    return output_path


def write_scenario_assessment_schema(path: str | Path) -> Path:
    """Write the current assessment JSON Schema."""
    return _write_schema(scenario_assessment_schema(), path)


def write_scenario_promotion_schema(path: str | Path) -> Path:
    """Write the current promotion JSON Schema."""
    return _write_schema(scenario_promotion_schema(), path)
