"""Tests for append-only scenario assessment and promotion contracts."""

from datetime import datetime, timezone
from importlib.resources import files
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from stormhub.scenarios import (
    FileReference,
    PublicationDisposition,
    QualificationStatus,
    ScenarioAssessment,
    ScenarioResponseIdentity,
    VersionedIdentity,
    load_scenario_assessment,
    load_scenario_promotion,
    new_scenario_assessment,
    new_scenario_promotion,
    scenario_assessment_schema,
    scenario_promotion_schema,
    write_scenario_assessment,
    write_scenario_promotion,
)

NOW = datetime(2026, 7, 30, tzinfo=timezone.utc)


def make_response() -> ScenarioResponseIdentity:
    """Create a reusable response identity fixture."""
    return ScenarioResponseIdentity(
        source_scenario_id="storm-rank-001",
        basin_id="deloutre",
        response_geometry_policy="valid-hydraulic-result-footprint",
        model_profile=VersionedIdentity(
            id="deloutre-profile",
            version="1.0.0",
            sha256="a" * 64,
        ),
        qualification_policy=VersionedIdentity(
            id="development-v0",
            version="0.1.0",
            sha256="b" * 64,
        ),
        dependency_resolution_sha256="c" * 64,
    )


def make_assessment(
    status: QualificationStatus = QualificationStatus.PASS,
) -> ScenarioAssessment:
    """Create a policy assessment record fixture."""
    reference = FileReference(
        asset_key="qualification-assessment",
        href="qualification-assessment.json",
        media_type="application/json",
        roles=["metadata", "quality"],
        sha256="d" * 64,
        size_bytes=100,
    )
    return new_scenario_assessment(
        run_id="scenario-001",
        specification_sha256="e" * 64,
        response=make_response(),
        assessment=reference,
        status=status,
        recommended_publication=(
            PublicationDisposition.CANDIDATE
            if status == QualificationStatus.PASS
            else PublicationDisposition.PROHIBITED
        ),
        created_at=NOW,
    )


def test_assessment_and_promotion_have_deterministic_content_identities() -> None:
    """Derive stable record IDs from every decision-defining field."""
    first = make_assessment()
    second = make_assessment()
    promotion = new_scenario_promotion(
        assessment=first,
        disposition=PublicationDisposition.ELIGIBLE,
        authority="forecast program manager",
        rationale="Independent review accepted the passing policy assessment.",
        decided_at=NOW,
    )

    assert first == second
    assert first.assessment_id.startswith("assessment-")
    assert promotion.promotion_id.startswith("promotion-")
    assert promotion.assessment_sha256 == first.sha256()


def test_positive_promotion_requires_pass_and_assessment_cannot_grant_eligibility() -> None:
    """Keep automated assessment separate from the final eligibility decision."""
    failed = make_assessment(QualificationStatus.FAIL)

    with pytest.raises(ValidationError, match="requires a passing assessment"):
        new_scenario_promotion(
            assessment=failed,
            disposition=PublicationDisposition.CANDIDATE,
            authority="operator",
            rationale="Invalid attempted promotion.",
            decided_at=NOW,
        )

    payload = make_assessment().model_dump()
    payload["recommended_publication"] = PublicationDisposition.ELIGIBLE
    payload["assessment_id"] = "assessment-invalid"
    with pytest.raises(ValidationError, match="cannot grant eligibility"):
        ScenarioAssessment.model_validate(payload)


def test_append_only_writers_allow_identical_retry_and_reject_replacement(tmp_path: Path) -> None:
    """Make decision persistence idempotent without allowing history rewrites."""
    assessment = make_assessment()
    assessment_path = tmp_path / "assessment.json"

    assert write_scenario_assessment(assessment, assessment_path) == assessment_path
    original = assessment_path.read_bytes()
    assert write_scenario_assessment(assessment, assessment_path) == assessment_path
    assert assessment_path.read_bytes() == original
    assessment_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Append-only"):
        write_scenario_assessment(assessment, assessment_path)

    promotion = new_scenario_promotion(
        assessment=assessment,
        disposition=PublicationDisposition.INTERNAL,
        authority="operator",
        rationale="Retained for engineering review.",
        decided_at=NOW,
    )
    promotion_path = tmp_path / "promotion.json"
    write_scenario_promotion(promotion, promotion_path)
    assert load_scenario_promotion(promotion_path) == promotion


def test_record_identity_detects_tampering(tmp_path: Path) -> None:
    """Reject a record whose content no longer matches its declared identity."""
    assessment = make_assessment()
    path = write_scenario_assessment(assessment, tmp_path / "assessment.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["assessment"]["sha256"] = "f" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValidationError, match="assessment_id does not match"):
        load_scenario_assessment(path)


def test_packaged_decision_schemas_match_python_models() -> None:
    """Fail when decision models change without regenerating their schemas."""
    root = files("stormhub.scenarios").joinpath("schemas")
    assessment = json.loads(
        root.joinpath("scenario-assessment-v1.0.0.schema.json").read_text(encoding="utf-8")
    )
    promotion = json.loads(
        root.joinpath("scenario-promotion-v1.0.0.schema.json").read_text(encoding="utf-8")
    )

    assert assessment == scenario_assessment_schema()
    assert promotion == scenario_promotion_schema()
