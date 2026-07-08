"""Adapters between StormHub STAC objects and scenario run contracts."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pystac

from stormhub.scenarios.contract import (
    AntecedentConditions,
    ExecutionSpec,
    FileReference,
    HydraulicModel,
    PrecipitationInput,
    ScenarioRunSpec,
    StacAssetReference,
    WatershedReference,
)
from stormhub.utils import sha256_file, sha256_from_checksum


def geometry_sha256(geometry: dict[str, Any]) -> str:
    """Hash a GeoJSON geometry using canonical JSON serialization."""
    canonical = json.dumps(geometry, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_remote_href(href: str) -> bool:
    """Return whether an href has a non-Windows URI scheme."""
    if re.match(r"^[A-Za-z]:[/\\]", href):
        return False
    return bool(urlsplit(href).scheme)


def _portable_href(href: str, manifest_path: str | Path) -> str:
    """Express a local href relative to the future manifest, preserving remote IRIs."""
    if _is_remote_href(href):
        return href
    try:
        relative = os.path.relpath(Path(href).resolve(), start=Path(manifest_path).resolve().parent)
    except ValueError as error:
        raise ValueError(f"Cannot make href portable relative to the scenario manifest: {href}") from error
    return Path(relative).as_posix()


def _required_asset(item: pystac.Item, asset_key: str) -> pystac.Asset:
    """Return a named asset with an actionable missing-asset error."""
    try:
        return item.assets[asset_key]
    except KeyError as error:
        available = ", ".join(sorted(item.assets)) or "none"
        raise ValueError(f"STAC Item '{item.id}' has no asset '{asset_key}'; available assets: {available}") from error


def _item_href(item: pystac.Item, manifest_path: str | Path) -> str:
    """Return a portable href for a saved STAC Item."""
    href = item.get_self_href()
    if href is None:
        raise ValueError(f"STAC Item '{item.id}' must be saved before creating a scenario run specification")
    return _portable_href(href, manifest_path)


def _asset_sha256(asset: pystac.Asset, *, verify_local: bool) -> str | None:
    """Read a STAC checksum and optionally verify the referenced local file."""
    recorded = sha256_from_checksum(asset.extra_fields.get("file:checksum"))
    absolute_href = asset.get_absolute_href()
    if absolute_href is None or _is_remote_href(absolute_href):
        return recorded

    path = Path(absolute_href)
    if not path.is_file():
        if verify_local:
            raise ValueError(f"Referenced local STAC asset does not exist: {absolute_href}")
        return recorded

    actual = sha256_file(path)
    if recorded is not None and verify_local and recorded != actual:
        raise ValueError(f"Checksum mismatch for STAC asset: {absolute_href}")
    return recorded or actual


def _stac_asset_reference(
    item: pystac.Item,
    asset_key: str,
    manifest_path: str | Path,
    *,
    verify_local: bool,
) -> StacAssetReference:
    """Translate a PySTAC Asset into a contract reference."""
    asset = _required_asset(item, asset_key)
    collection_id = item.collection_id
    if collection_id is None:
        raise ValueError(f"STAC Item '{item.id}' must belong to a collection")
    absolute_asset_href = asset.get_absolute_href() or asset.href
    return StacAssetReference(
        item_href=_item_href(item, manifest_path),
        collection_id=collection_id,
        item_id=item.id,
        asset_key=asset_key,
        asset_href=_portable_href(absolute_asset_href, manifest_path),
        sha256=_asset_sha256(asset, verify_local=verify_local),
    )


def file_reference_from_path(
    path: str | Path,
    manifest_path: str | Path,
    *,
    media_type: str,
    roles: list[str],
    title: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> FileReference:
    """Create a content-addressed contract reference for a local file."""
    file_path = Path(path).resolve()
    if not file_path.is_file():
        raise ValueError(f"Scenario artifact does not exist: {file_path}")
    return FileReference(
        href=_portable_href(str(file_path), manifest_path),
        media_type=media_type,
        roles=roles,
        sha256=sha256_file(file_path),
        size_bytes=file_path.stat().st_size,
        title=title or file_path.name,
        metadata=metadata or {},
    )


def scenario_run_spec_from_stac(
    storm_item: pystac.Item,
    watershed_item: pystac.Item,
    hydraulic_model: HydraulicModel,
    execution: ExecutionSpec,
    manifest_path: str | Path,
    *,
    source_asset_key: str = "AORC",
    target_dss_asset_key: str = "dss-target",
    antecedent_conditions: AntecedentConditions | None = None,
    require_dss_validation: bool = True,
    verify_local_checksums: bool = True,
) -> ScenarioRunSpec:
    """Build a validated run specification from a StormHub event and watershed.

    The adapter is intentionally strict at the hydraulic submission boundary.
    It rejects a target DSS associated with another watershed, failed or missing
    DSS validation, missing event dates, and local checksum mismatches.
    """
    source_asset = _required_asset(storm_item, source_asset_key)
    target_asset = _required_asset(storm_item, target_dss_asset_key)

    if target_asset.extra_fields.get("stormhub:spatial_role") != "target":
        raise ValueError(f"Asset '{target_dss_asset_key}' is not marked as a target-transposed DSS")
    target_watershed_id = target_asset.extra_fields.get("stormhub:target_watershed_id")
    if target_watershed_id != watershed_item.id:
        raise ValueError(
            f"Target DSS watershed '{target_watershed_id}' does not match requested watershed '{watershed_item.id}'"
        )
    validation_status = target_asset.extra_fields.get("stormhub:validation_status", "not_run")
    if require_dss_validation and validation_status != "passed":
        raise ValueError(f"Target DSS validation status must be 'passed', not '{validation_status}'")

    start = storm_item.common_metadata.start_datetime
    end = storm_item.common_metadata.end_datetime
    if start is None or end is None:
        raise ValueError(f"Storm Item '{storm_item.id}' must have start_datetime and end_datetime")
    duration = (end - start).total_seconds() / 3600
    if not duration.is_integer():
        raise ValueError(f"Storm Item '{storm_item.id}' duration must be a whole number of hours")

    source_reference = _stac_asset_reference(
        storm_item,
        source_asset_key,
        manifest_path,
        verify_local=verify_local_checksums and not _is_remote_href(source_asset.href),
    )
    target_reference = _stac_asset_reference(
        storm_item,
        target_dss_asset_key,
        manifest_path,
        verify_local=verify_local_checksums,
    )
    rank = storm_item.properties.get("aorc:collection_rank")

    return ScenarioRunSpec(
        watershed=WatershedReference(
            watershed_id=watershed_item.id,
            item_href=_item_href(watershed_item, manifest_path),
            geometry_sha256=geometry_sha256(watershed_item.geometry),
        ),
        precipitation=PrecipitationInput(
            source_aorc=source_reference,
            transposed_dss=target_reference,
            event_start=start,
            event_end=end,
            duration_hours=int(duration),
            rank=int(rank) if rank is not None else None,
        ),
        hydraulic_model=hydraulic_model,
        execution=execution,
        antecedent_conditions=antecedent_conditions,
    )
