"""Publish completed flood scenario runs as portable STAC Items."""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import pystac
from pystac.extensions.file import FileExtension
from pystac.extensions.item_assets import ItemAssetsExtension
from pystac.extensions.raster import RasterExtension

from stormhub.scenarios.contract import (
    FileReference,
    PublicationDisposition,
    RunStatus,
    ScenarioRun,
    write_scenario_run,
)
from stormhub.scenarios.response import (
    ScenarioAssessment,
    ScenarioPromotion,
    write_scenario_assessment,
    write_scenario_promotion,
)
from stormhub.scenarios.stac import geometry_sha256
from stormhub.utils import sha256_file, sha256_multihash

DEFAULT_COLLECTION_ID = "flood-scenario-runs"
DEFAULT_COLLECTION_DESCRIPTION = (
    "Hydrologic-hydraulic model executions derived from StormHub precipitation scenarios."
)
DEFAULT_ASSESSMENT_COLLECTION_ID = "flood-scenario-assessments"
DEFAULT_ASSESSMENT_COLLECTION_DESCRIPTION = (
    "Append-only policy assessments of immutable flood scenario response runs."
)
DEFAULT_PROMOTION_COLLECTION_ID = "flood-scenario-promotions"
DEFAULT_PROMOTION_COLLECTION_DESCRIPTION = (
    "Append-only operator publication decisions for assessed flood scenario responses."
)
TARGET_PRECIPITATION_ASSET_KEY = "target-precipitation-dss"
TABLE_EXTENSION_SCHEMA = "https://stac-extensions.github.io/table/v1.2.0/schema.json"
PROJECTION_EXTENSION_SCHEMA = "https://stac-extensions.github.io/projection/v2.0.0/schema.json"
ITEM_ASSETS_EXTENSION_SCHEMA = ItemAssetsExtension.get_schema_uri()

SCENARIO_RESPONSE_ITEM_ASSETS = {
    "scenario-run": {
        "title": "Scenario run manifest",
        "description": "Versioned immutable-input and terminal-execution contract.",
        "type": pystac.MediaType.JSON,
        "roles": ["metadata"],
    },
    "scenario-products": {
        "title": "Scenario product manifest",
        "description": "Portable identities and metadata for the assembled response products.",
        "type": pystac.MediaType.JSON,
        "roles": ["metadata", "product-manifest"],
    },
    "qualification": {
        "title": "Scenario qualification",
        "description": "Evidence supporting the four integrated qualification gates.",
        "type": pystac.MediaType.JSON,
        "roles": ["metadata", "quality"],
    },
    TARGET_PRECIPITATION_ASSET_KEY: {
        "title": "Watershed-transposed precipitation DSS",
        "description": "StormHub precipitation forcing used by the hydrologic-hydraulic run.",
        "type": "application/x-dss",
        "roles": ["data", "input", "precipitation"],
    },
    "hms-output-dss": {
        "title": "HEC-HMS output DSS",
        "description": "Authoritative HEC-HMS results used for the hydraulic handoff.",
        "type": "application/x-dss",
        "roles": ["data", "hydrologic-output"],
    },
    "hms-pathname-catalog": {
        "title": "HMS pathname qualification catalog",
        "description": "Qualified DSS pathname inventory for required HEC-RAS boundaries.",
        "type": pystac.MediaType.JSON,
        "roles": ["metadata", "quality", "hydrologic-handoff"],
    },
    "hms-hydrographs": {
        "title": "HMS hydrographs",
        "description": "Portable hydrologic time series for the required boundary mappings.",
        "type": "application/vnd.apache.parquet",
        "roles": ["data", "hydrograph", "hydrologic-output"],
    },
    "ras-result-hdf": {
        "title": "HEC-RAS result HDF",
        "description": "Authoritative HEC-RAS plan result.",
        "type": "application/x-hdf5",
        "roles": ["data", "hydraulic-output", "engineering-result"],
    },
    "ras-hydrographs": {
        "title": "RAS hydrographs",
        "description": "Portable hydraulic boundary flow and stage time series.",
        "type": "application/vnd.apache.parquet",
        "roles": ["data", "hydrograph", "hydraulic-output"],
    },
    "maximum-wse": {
        "title": "Maximum water-surface elevation",
        "description": "Cloud-optimized GeoTIFF of maximum modeled water-surface elevation.",
        "type": "image/tiff; application=geotiff; profile=cloud-optimized",
        "roles": ["data", "visual", "maximum-wse"],
    },
    "maximum-depth": {
        "title": "Maximum depth",
        "description": "Cloud-optimized GeoTIFF of maximum modeled depth.",
        "type": "image/tiff; application=geotiff; profile=cloud-optimized",
        "roles": ["data", "visual", "maximum-depth"],
    },
    "maximum-velocity": {
        "title": "Maximum velocity",
        "description": "Cloud-optimized GeoTIFF of maximum modeled velocity.",
        "type": "image/tiff; application=geotiff; profile=cloud-optimized",
        "roles": ["data", "visual", "maximum-velocity"],
    },
    "hydraulic-time-cube": {
        "title": "Hydraulic time cube",
        "description": "Validated chunked time-varying hydraulic raster product.",
        "type": "application/vnd+zarr",
        "roles": ["data", "hydraulic-output"],
    },
    "preview": {
        "title": "Scenario preview",
        "description": "Browse image for rapid inspection of the scenario response.",
        "type": "image/png",
        "roles": ["overview", "visual"],
    },
}

ASSESSMENT_ITEM_ASSETS = {
    "scenario-assessment": {
        "title": "Scenario assessment record",
        "description": "Immutable link between a scenario response and a policy assessment.",
        "type": pystac.MediaType.JSON,
        "roles": ["metadata", "quality"],
    },
    "qualification-assessment": {
        "title": "Qualification assessment",
        "description": "Policy-owned metrics, thresholds, and evaluation results.",
        "type": pystac.MediaType.JSON,
        "roles": ["metadata", "quality"],
    },
}

PROMOTION_ITEM_ASSETS = {
    "scenario-promotion": {
        "title": "Scenario promotion record",
        "description": "Immutable operator decision governing response publication.",
        "type": pystac.MediaType.JSON,
        "roles": ["metadata", "decision"],
    },
}


def _is_remote_href(href: str) -> bool:
    """Return whether an href has a non-Windows URI scheme."""
    if re.match(r"^[A-Za-z]:[/\\]", href):
        return False
    return bool(urlsplit(href).scheme)


def _resolve_contract_href(href: str, manifest_path: Path) -> str:
    """Resolve a manifest-relative href while preserving remote IRIs."""
    if _is_remote_href(href):
        return href
    return str((manifest_path.parent / Path(href)).resolve())


def _publication_href(href: str, manifest_path: Path, item_path: Path) -> str:
    """Rebase a contract href so it is portable from a STAC Item."""
    resolved = _resolve_contract_href(href, manifest_path)
    if _is_remote_href(resolved):
        return resolved
    try:
        relative = os.path.relpath(resolved, start=item_path.parent.resolve())
    except ValueError as error:
        raise ValueError(f"Cannot publish href relative to STAC Item: {href}") from error
    return Path(relative).as_posix()


def _verify_file_reference(reference: FileReference, manifest_path: Path) -> None:
    """Verify a local contract file against its recorded size and checksum."""
    resolved = _resolve_contract_href(reference.href, manifest_path)
    if _is_remote_href(resolved):
        return
    path = Path(resolved)
    if not path.is_file():
        raise ValueError(f"Scenario asset '{reference.asset_key}' does not exist: {path}")
    if path.stat().st_size != reference.size_bytes:
        raise ValueError(f"Size mismatch for scenario asset '{reference.asset_key}': {path}")
    if sha256_file(path) != reference.sha256:
        raise ValueError(f"Checksum mismatch for scenario asset '{reference.asset_key}': {path}")


def _file_asset(reference: FileReference, manifest_path: Path, item_path: Path) -> pystac.Asset:
    """Translate a contract file reference into a checksum-pinned STAC Asset."""
    extra_fields = dict(reference.metadata)
    extra_fields.update(
        {
            "file:size": reference.size_bytes,
            "file:checksum": sha256_multihash(reference.sha256),
        }
    )
    return pystac.Asset(
        href=_publication_href(reference.href, manifest_path, item_path),
        title=reference.title,
        media_type=reference.media_type,
        roles=list(reference.roles),
        extra_fields=extra_fields,
    )


def _declare_asset_extensions(item: pystac.Item, references: list[FileReference]) -> None:
    """Declare known STAC extensions used by contract asset metadata."""
    metadata_keys = {key for reference in references for key in reference.metadata}
    if any(key.startswith("proj:") for key in metadata_keys):
        item.stac_extensions.append(PROJECTION_EXTENSION_SCHEMA)
    if any(key.startswith("raster:") for key in metadata_keys):
        RasterExtension.add_to(item)
    if any(key.startswith("table:") for key in metadata_keys):
        item.stac_extensions.append(TABLE_EXTENSION_SCHEMA)


def _item_asset_definition(asset: pystac.Asset) -> dict[str, object]:
    """Build a Collection definition for an observed nonstandard asset key."""
    definition: dict[str, object] = {}
    if asset.title:
        definition["title"] = asset.title
    if asset.media_type:
        definition["type"] = asset.media_type
    if asset.roles:
        definition["roles"] = list(asset.roles)
    return definition


def _update_collection_item_assets(
    collection: pystac.Collection,
    item: pystac.Item,
    expected: dict[str, dict[str, object]] = SCENARIO_RESPONSE_ITEM_ASSETS,
) -> None:
    """Maintain the stable union of expected and observed Collection assets."""
    definitions = dict(collection.extra_fields.get("item_assets", {}))
    definitions.update({key: dict(definition) for key, definition in expected.items()})
    for key, asset in item.assets.items():
        definitions.setdefault(key, _item_asset_definition(asset))
    collection.extra_fields["item_assets"] = definitions
    if ITEM_ASSETS_EXTENSION_SCHEMA not in collection.stac_extensions:
        collection.stac_extensions.append(ITEM_ASSETS_EXTENSION_SCHEMA)


def _validate_publishable_run(run: ScenarioRun, watershed_item: pystac.Item) -> None:
    """Enforce terminal state and watershed identity at publication time."""
    terminal = {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}
    if run.status not in terminal:
        raise ValueError(f"Only terminal scenario runs can be published, not '{run.status.value}'")
    if watershed_item.id != run.spec.watershed.watershed_id:
        raise ValueError(
            f"Watershed Item '{watershed_item.id}' does not match run watershed '{run.spec.watershed.watershed_id}'"
        )
    if geometry_sha256(watershed_item.geometry) != run.spec.watershed.geometry_sha256:
        raise ValueError(f"Watershed geometry has changed for scenario run '{run.run_id}'")


def scenario_run_to_stac_item(
    run: ScenarioRun,
    manifest_path: str | Path,
    item_path: str | Path,
    watershed_item: pystac.Item,
    *,
    collection_id: str = DEFAULT_COLLECTION_ID,
    verify_files: bool = True,
) -> pystac.Item:
    """Create a STAC Item for a terminal flood scenario run.

    The manifest must already exist at ``manifest_path``. Local model packages,
    outputs, and the target DSS are verified before their Assets are emitted.
    """
    manifest = Path(manifest_path).resolve()
    output_item_path = Path(item_path).resolve()
    if not manifest.is_file():
        raise ValueError(f"Scenario run manifest does not exist: {manifest}")
    _validate_publishable_run(run, watershed_item)

    references = [run.spec.hydraulic.model.package, *run.outputs]
    if run.spec.hydrologic is not None:
        references.insert(0, run.spec.hydrologic.model.package)
    if verify_files:
        for reference in references:
            _verify_file_reference(reference, manifest)

    target_dss = run.spec.precipitation.transposed_dss
    if target_dss.asset_href is None or target_dss.sha256 is None:
        raise ValueError("The transposed DSS reference must include asset_href and sha256")
    resolved_dss = _resolve_contract_href(target_dss.asset_href, manifest)
    dss_size = None
    if not _is_remote_href(resolved_dss):
        dss_path = Path(resolved_dss)
        if not dss_path.is_file():
            raise ValueError(f"Transposed DSS does not exist: {dss_path}")
        dss_size = dss_path.stat().st_size
        if verify_files and sha256_file(dss_path) != target_dss.sha256:
            raise ValueError(f"Checksum mismatch for transposed DSS: {dss_path}")

    properties = {
        "stormhub:contract_version": run.contract_version,
        "stormhub:workflow": run.spec.workflow.value,
        "stormhub:run_status": run.status.value,
        "stormhub:specification_sha256": run.specification_sha256,
        "stormhub:created_at": run.created_at.isoformat(),
        "stormhub:started_at": run.started_at.isoformat() if run.started_at else None,
        "stormhub:completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "stormhub:watershed_id": run.spec.watershed.watershed_id,
        "stormhub:precipitation_item_id": run.spec.precipitation.source_aorc.item_id,
        "stormhub:precipitation_collection_id": run.spec.precipitation.source_aorc.collection_id,
        "stormhub:hydraulic_model_id": run.spec.hydraulic.model.model_id,
        "stormhub:hydraulic_model_version": run.spec.hydraulic.model.model_version,
        "stormhub:engine": run.spec.hydraulic.model.engine,
        "stormhub:engine_version": run.spec.hydraulic.model.engine_version,
        "stormhub:plan_name": run.spec.hydraulic.model.plan_name,
        "stormhub:stage_statuses": {
            stage.stage.value: stage.status.value for stage in run.stages
        },
        "stormhub:quality_status": run.quality.status.value,
        "stormhub:hms_execution": run.qualification.hms_execution.status.value,
        "stormhub:hydrologic_handoff": run.qualification.hydrologic_handoff.status.value,
        "stormhub:ras_execution": run.qualification.ras_execution.status.value,
        "stormhub:hydraulic_qaqc": run.qualification.hydraulic_qaqc.status.value,
        "stormhub:publication": run.publication.value,
        "stormhub:forecast_eligible": run.publication == PublicationDisposition.ELIGIBLE,
        "stormhub:labels": run.labels,
    }
    if run.spec.response is not None:
        response = run.spec.response
        properties.update(
            {
                "stormhub:source_scenario_id": response.source_scenario_id,
                "stormhub:basin_id": response.basin_id,
                "stormhub:response_geometry_policy": response.response_geometry_policy,
                "stormhub:model_profile_id": response.model_profile.id,
                "stormhub:model_profile_version": response.model_profile.version,
                "stormhub:model_profile_sha256": response.model_profile.sha256,
                "stormhub:qualification_policy_id": response.qualification_policy.id,
                "stormhub:qualification_policy_version": response.qualification_policy.version,
                "stormhub:qualification_policy_sha256": response.qualification_policy.sha256,
                "stormhub:dependency_resolution_sha256": response.dependency_resolution_sha256,
            }
        )
        if response.compatibility_certificate is not None:
            certificate = response.compatibility_certificate
            properties.update(
                {
                    "stormhub:compatibility_certificate_id": certificate.id,
                    "stormhub:compatibility_certificate_version": certificate.version,
                    "stormhub:compatibility_certificate_sha256": certificate.sha256,
                }
            )
    if run.spec.hydrologic is not None:
        properties.update(
            {
                "stormhub:hydrologic_model_id": run.spec.hydrologic.model.model_id,
                "stormhub:hydrologic_model_version": run.spec.hydrologic.model.model_version,
                "stormhub:hydrologic_engine": run.spec.hydrologic.model.engine,
                "stormhub:hydrologic_engine_version": run.spec.hydrologic.model.engine_version,
                "stormhub:hms_run_name": run.spec.hydrologic.model.run_name,
            }
        )
    if run.failure is not None:
        properties["stormhub:failure"] = {
            "error_type": run.failure.error_type,
            "retryable": run.failure.retryable,
        }

    item = pystac.Item(
        id=run.run_id,
        geometry=watershed_item.geometry,
        bbox=watershed_item.bbox,
        datetime=None,
        properties=properties,
        start_datetime=run.spec.precipitation.event_start,
        end_datetime=run.spec.precipitation.event_end,
        collection=collection_id,
    )
    item.set_self_href(str(output_item_path))
    FileExtension.add_to(item)
    _declare_asset_extensions(item, references)

    item.add_link(
        pystac.Link(
            rel=pystac.RelType.DERIVED_FROM,
            target=_publication_href(run.spec.precipitation.source_aorc.item_href, manifest, output_item_path),
            media_type=pystac.MediaType.JSON,
            title=f"Precipitation scenario {run.spec.precipitation.source_aorc.item_id}",
        )
    )
    item.add_link(
        pystac.Link(
            rel="related",
            target=_publication_href(run.spec.watershed.item_href, manifest, output_item_path),
            media_type=pystac.MediaType.GEOJSON,
            title=f"Watershed {run.spec.watershed.watershed_id}",
        )
    )

    manifest_sha256 = sha256_file(manifest)
    item.add_asset(
        "scenario-run",
        pystac.Asset(
            href=_publication_href(str(manifest), manifest, output_item_path),
            title="Scenario Run Manifest",
            media_type=pystac.MediaType.JSON,
            roles=["metadata"],
            extra_fields={
                "file:size": manifest.stat().st_size,
                "file:checksum": sha256_multihash(manifest_sha256),
            },
        ),
    )

    dss_fields = {"file:checksum": sha256_multihash(target_dss.sha256)}
    if dss_size is not None:
        dss_fields["file:size"] = dss_size
    item.add_asset(
        TARGET_PRECIPITATION_ASSET_KEY,
        pystac.Asset(
            href=_publication_href(target_dss.asset_href, manifest, output_item_path),
            title="Watershed-transposed precipitation DSS",
            media_type="application/x-dss",
            roles=["data", "input", "precipitation"],
            extra_fields={
                **dss_fields,
                "stormhub:source_asset_key": target_dss.asset_key,
            },
        ),
    )
    item.add_asset(
        run.spec.hydraulic.model.package.asset_key,
        _file_asset(run.spec.hydraulic.model.package, manifest, output_item_path),
    )
    if run.spec.hydrologic is not None:
        item.add_asset(
            run.spec.hydrologic.model.package.asset_key,
            _file_asset(run.spec.hydrologic.model.package, manifest, output_item_path),
        )
    for output in run.outputs:
        item.add_asset(output.asset_key, _file_asset(output, manifest, output_item_path))
    return item


def _new_collection(
    collection_id: str,
    description: str,
    item: pystac.Item,
    collection_path: Path,
    license: str,
) -> pystac.Collection:
    """Create a flood-scenario Collection initialized from its first Item."""
    extent = pystac.Extent(
        spatial=pystac.SpatialExtent([item.bbox]),
        temporal=pystac.TemporalExtent([[item.common_metadata.start_datetime, item.common_metadata.end_datetime]]),
    )
    collection = pystac.Collection(
        id=collection_id,
        description=description,
        extent=extent,
        license=license,
    )
    collection.set_self_href(str(collection_path))
    return collection


def _response_properties(run: ScenarioRun) -> dict[str, object]:
    """Return the shared searchable identity for response-aware records."""
    response = run.spec.response
    if response is None:
        raise ValueError(f"Scenario run '{run.run_id}' has no response identity")
    properties: dict[str, object] = {
        "stormhub:source_scenario_id": response.source_scenario_id,
        "stormhub:basin_id": response.basin_id,
        "stormhub:response_geometry_policy": response.response_geometry_policy,
        "stormhub:model_profile_id": response.model_profile.id,
        "stormhub:model_profile_version": response.model_profile.version,
        "stormhub:model_profile_sha256": response.model_profile.sha256,
        "stormhub:qualification_policy_id": response.qualification_policy.id,
        "stormhub:qualification_policy_version": response.qualification_policy.version,
        "stormhub:qualification_policy_sha256": response.qualification_policy.sha256,
        "stormhub:dependency_resolution_sha256": response.dependency_resolution_sha256,
    }
    if response.compatibility_certificate is not None:
        certificate = response.compatibility_certificate
        properties.update(
            {
                "stormhub:compatibility_certificate_id": certificate.id,
                "stormhub:compatibility_certificate_version": certificate.version,
                "stormhub:compatibility_certificate_sha256": certificate.sha256,
            }
        )
    return properties


def _validate_response_record(
    *,
    run: ScenarioRun,
    run_item: pystac.Item,
    run_id: str,
    specification_sha256: str,
    response: object,
) -> None:
    """Authenticate a decision record against the immutable run and STAC Item."""
    if run.status != RunStatus.SUCCEEDED:
        raise ValueError("Only succeeded response runs can be assessed or promoted")
    if run.run_id != run_id or run.specification_sha256 != specification_sha256:
        raise ValueError("Response record does not match the scenario run identity")
    if run.spec.response != response:
        raise ValueError("Response record does not match the scenario response identity")
    if run_item.id != run.run_id:
        raise ValueError("Scenario run Item does not match the scenario run")
    for key, value in _response_properties(run).items():
        if run_item.properties.get(key) != value:
            raise ValueError(f"Scenario run Item response property does not match: {key}")


def _record_asset(
    path: Path,
    item_path: Path,
    *,
    title: str,
    roles: list[str],
) -> pystac.Asset:
    """Create a checksum-pinned STAC Asset for one immutable JSON record."""
    digest = sha256_file(path)
    return pystac.Asset(
        href=_publication_href(str(path), path, item_path),
        title=title,
        media_type=pystac.MediaType.JSON,
        roles=roles,
        extra_fields={
            "file:size": path.stat().st_size,
            "file:checksum": sha256_multihash(digest),
        },
    )


def _decision_item(
    *,
    item_id: str,
    run_item: pystac.Item,
    collection_id: str,
    properties: dict[str, object],
    item_path: Path,
) -> pystac.Item:
    """Create a decision Item with the run's spatial and temporal extent."""
    item = pystac.Item(
        id=item_id,
        geometry=run_item.geometry,
        bbox=run_item.bbox,
        datetime=None,
        properties=properties,
        start_datetime=run_item.common_metadata.start_datetime,
        end_datetime=run_item.common_metadata.end_datetime,
        collection=collection_id,
    )
    item.set_self_href(str(item_path))
    FileExtension.add_to(item)
    return item


def _publish_decision_item(
    *,
    catalog: pystac.Catalog,
    item: pystac.Item,
    collection_id: str,
    collection_description: str,
    expected_assets: dict[str, dict[str, object]],
    license: str,
) -> pystac.Item:
    """Append one immutable decision Item and update portable catalog links."""
    catalog_href = catalog.get_self_href()
    if catalog_href is None or _is_remote_href(catalog_href):
        raise ValueError("Publishing currently requires a saved local STAC Catalog")
    catalog_dir = Path(catalog_href).resolve().parent
    collection_path = catalog_dir / collection_id / "collection.json"
    collection = catalog.get_child(collection_id)
    if collection is not None and not isinstance(collection, pystac.Collection):
        raise ValueError(f"Catalog child '{collection_id}' is not a STAC Collection")
    existing = collection.get_item(item.id, recursive=False) if collection is not None else None
    if existing is not None:
        raise ValueError(f"Decision Item '{item.id}' is already published with different content")
    if Path(item.get_self_href()).exists():
        raise ValueError(f"Decision Item path already exists: {item.get_self_href()}")

    if collection is None:
        collection = _new_collection(
            collection_id,
            collection_description,
            item,
            collection_path,
            license,
        )
        catalog.add_child(collection)
    _update_collection_item_assets(collection, item, expected_assets)
    collection.add_item(item)
    item.save_object(include_self_link=False)
    collection.update_extent_from_items()
    collection.save_object(include_self_link=False)
    catalog.save_object(include_self_link=False)
    return item


def publish_scenario_run(
    catalog: pystac.Catalog,
    run: ScenarioRun,
    manifest_path: str | Path,
    watershed_item: pystac.Item,
    *,
    collection_id: str = DEFAULT_COLLECTION_ID,
    collection_description: str = DEFAULT_COLLECTION_DESCRIPTION,
    license: str = "proprietary",
    item_path: str | Path | None = None,
    verify_files: bool = True,
    overwrite: bool = False,
) -> pystac.Item:
    """Write a run manifest and publish its STAC Item into a local catalog."""
    _validate_publishable_run(run, watershed_item)
    catalog_href = catalog.get_self_href()
    if catalog_href is None or _is_remote_href(catalog_href):
        raise ValueError("Publishing currently requires a saved local STAC Catalog")
    catalog_dir = Path(catalog_href).resolve().parent
    collection_dir = catalog_dir / collection_id
    collection_path = collection_dir / "collection.json"
    output_item_path = (
        Path(item_path).resolve() if item_path is not None else collection_dir / run.run_id / f"{run.run_id}.json"
    )

    collection = catalog.get_child(collection_id)
    if collection is not None and not isinstance(collection, pystac.Collection):
        raise ValueError(f"Catalog child '{collection_id}' is not a STAC Collection")
    existing = collection.get_item(run.run_id, recursive=False) if collection is not None else None
    if (existing is not None or output_item_path.exists()) and not overwrite:
        raise ValueError(f"Scenario run '{run.run_id}' is already published")
    if existing is not None and overwrite:
        existing_digest = existing.properties.get("stormhub:specification_sha256")
        if existing_digest != run.specification_sha256:
            raise ValueError(f"Cannot overwrite scenario run '{run.run_id}' with a different specification digest")

    write_scenario_run(run, manifest_path)
    item = scenario_run_to_stac_item(
        run,
        manifest_path,
        output_item_path,
        watershed_item,
        collection_id=collection_id,
        verify_files=verify_files,
    )

    if collection is None:
        collection = _new_collection(collection_id, collection_description, item, collection_path, license)
        catalog.add_child(collection)
    elif existing is not None:
        collection.remove_item(run.run_id)

    _update_collection_item_assets(collection, item)
    collection.add_item(item)
    item.save_object(include_self_link=False)
    collection.update_extent_from_items()
    collection.save_object(include_self_link=False)
    catalog.save_object(include_self_link=False)
    return item


def publish_scenario_assessment(
    catalog: pystac.Catalog,
    run: ScenarioRun,
    run_item: pystac.Item,
    assessment: ScenarioAssessment,
    record_path: str | Path,
    *,
    collection_id: str = DEFAULT_ASSESSMENT_COLLECTION_ID,
    collection_description: str = DEFAULT_ASSESSMENT_COLLECTION_DESCRIPTION,
    license: str = "proprietary",
    item_path: str | Path | None = None,
    verify_files: bool = True,
) -> pystac.Item:
    """Publish an append-only qualification assessment without changing the run."""
    _validate_response_record(
        run=run,
        run_item=run_item,
        run_id=assessment.run_id,
        specification_sha256=assessment.specification_sha256,
        response=assessment.response,
    )
    catalog_href = catalog.get_self_href()
    if catalog_href is None or _is_remote_href(catalog_href):
        raise ValueError("Publishing currently requires a saved local STAC Catalog")
    catalog_dir = Path(catalog_href).resolve().parent
    output_item_path = (
        Path(item_path).resolve()
        if item_path is not None
        else catalog_dir
        / collection_id
        / assessment.assessment_id
        / f"{assessment.assessment_id}.json"
    )
    record = write_scenario_assessment(assessment, record_path).resolve()
    if verify_files:
        _verify_file_reference(assessment.assessment, record)

    collection = catalog.get_child(collection_id)
    existing = (
        collection.get_item(assessment.assessment_id, recursive=False)
        if isinstance(collection, pystac.Collection)
        else None
    )
    if existing is not None:
        if existing.properties.get("stormhub:assessment_sha256") == assessment.sha256():
            return existing
        raise ValueError(
            f"Assessment Item '{assessment.assessment_id}' is already published with different content"
        )

    properties = {
        **_response_properties(run),
        "stormhub:assessment_contract_version": assessment.contract_version,
        "stormhub:run_id": assessment.run_id,
        "stormhub:specification_sha256": assessment.specification_sha256,
        "stormhub:assessment_status": assessment.status.value,
        "stormhub:recommended_publication": assessment.recommended_publication.value,
        "stormhub:assessment_sha256": assessment.sha256(),
        "stormhub:created_at": assessment.created_at.isoformat(),
        "stormhub:forecast_eligible": False,
    }
    item = _decision_item(
        item_id=assessment.assessment_id,
        run_item=run_item,
        collection_id=collection_id,
        properties=properties,
        item_path=output_item_path,
    )
    run_href = run_item.get_self_href()
    if run_href is None:
        raise ValueError("Scenario run Item must have a self href")
    item.add_link(
        pystac.Link(
            rel=pystac.RelType.DERIVED_FROM,
            target=_publication_href(run_href, record, output_item_path),
            media_type=pystac.MediaType.JSON,
            title=f"Scenario run {run.run_id}",
        )
    )
    item.add_asset(
        "scenario-assessment",
        _record_asset(
            record,
            output_item_path,
            title="Scenario assessment record",
            roles=["metadata", "quality"],
        ),
    )
    item.add_asset(
        assessment.assessment.asset_key,
        _file_asset(assessment.assessment, record, output_item_path),
    )
    _declare_asset_extensions(item, [assessment.assessment])
    return _publish_decision_item(
        catalog=catalog,
        item=item,
        collection_id=collection_id,
        collection_description=collection_description,
        expected_assets=ASSESSMENT_ITEM_ASSETS,
        license=license,
    )


def publish_scenario_promotion(
    catalog: pystac.Catalog,
    run: ScenarioRun,
    run_item: pystac.Item,
    assessment: ScenarioAssessment,
    assessment_item: pystac.Item,
    promotion: ScenarioPromotion,
    record_path: str | Path,
    *,
    collection_id: str = DEFAULT_PROMOTION_COLLECTION_ID,
    collection_description: str = DEFAULT_PROMOTION_COLLECTION_DESCRIPTION,
    license: str = "proprietary",
    item_path: str | Path | None = None,
    superseded_item: pystac.Item | None = None,
    verify_files: bool = True,
) -> pystac.Item:
    """Publish an append-only promotion decision without mutating prior Items."""
    _validate_response_record(
        run=run,
        run_item=run_item,
        run_id=promotion.run_id,
        specification_sha256=promotion.specification_sha256,
        response=promotion.response,
    )
    if (
        promotion.assessment_id != assessment.assessment_id
        or promotion.assessment_sha256 != assessment.sha256()
        or promotion.assessment_status != assessment.status
    ):
        raise ValueError("Promotion does not match its assessment record")
    if assessment_item.id != assessment.assessment_id:
        raise ValueError("Assessment Item does not match the assessment record")
    if assessment_item.properties.get("stormhub:assessment_sha256") != assessment.sha256():
        raise ValueError("Assessment Item digest does not match the assessment record")
    if promotion.supersedes_promotion_id is not None:
        if superseded_item is None or superseded_item.id != promotion.supersedes_promotion_id:
            raise ValueError("A superseded promotion requires its matching predecessor Item")
    elif superseded_item is not None:
        raise ValueError("A predecessor Item requires supersedes_promotion_id")

    catalog_href = catalog.get_self_href()
    if catalog_href is None or _is_remote_href(catalog_href):
        raise ValueError("Publishing currently requires a saved local STAC Catalog")
    catalog_dir = Path(catalog_href).resolve().parent
    output_item_path = (
        Path(item_path).resolve()
        if item_path is not None
        else catalog_dir
        / collection_id
        / promotion.promotion_id
        / f"{promotion.promotion_id}.json"
    )
    record = write_scenario_promotion(promotion, record_path).resolve()
    if verify_files:
        for reference in promotion.evidence:
            _verify_file_reference(reference, record)

    collection = catalog.get_child(collection_id)
    existing = (
        collection.get_item(promotion.promotion_id, recursive=False)
        if isinstance(collection, pystac.Collection)
        else None
    )
    if existing is not None:
        if existing.properties.get("stormhub:promotion_sha256") == promotion.sha256():
            return existing
        raise ValueError(
            f"Promotion Item '{promotion.promotion_id}' is already published with different content"
        )

    properties = {
        **_response_properties(run),
        "stormhub:promotion_contract_version": promotion.contract_version,
        "stormhub:run_id": promotion.run_id,
        "stormhub:specification_sha256": promotion.specification_sha256,
        "stormhub:assessment_id": promotion.assessment_id,
        "stormhub:assessment_sha256": promotion.assessment_sha256,
        "stormhub:assessment_status": promotion.assessment_status.value,
        "stormhub:publication": promotion.disposition.value,
        "stormhub:forecast_eligible": (
            promotion.disposition == PublicationDisposition.ELIGIBLE
        ),
        "stormhub:promotion_sha256": promotion.sha256(),
        "stormhub:decided_at": promotion.decided_at.isoformat(),
        "stormhub:authority": promotion.authority,
        "stormhub:supersedes_promotion_id": promotion.supersedes_promotion_id,
    }
    item = _decision_item(
        item_id=promotion.promotion_id,
        run_item=run_item,
        collection_id=collection_id,
        properties=properties,
        item_path=output_item_path,
    )
    assessment_href = assessment_item.get_self_href()
    run_href = run_item.get_self_href()
    if assessment_href is None or run_href is None:
        raise ValueError("Run and assessment Items must have self hrefs")
    item.add_link(
        pystac.Link(
            rel=pystac.RelType.DERIVED_FROM,
            target=_publication_href(assessment_href, record, output_item_path),
            media_type=pystac.MediaType.JSON,
            title=f"Scenario assessment {assessment.assessment_id}",
        )
    )
    item.add_link(
        pystac.Link(
            rel="related",
            target=_publication_href(run_href, record, output_item_path),
            media_type=pystac.MediaType.JSON,
            title=f"Scenario run {run.run_id}",
        )
    )
    if superseded_item is not None:
        predecessor_href = superseded_item.get_self_href()
        if predecessor_href is None:
            raise ValueError("Superseded promotion Item must have a self href")
        item.add_link(
            pystac.Link(
                rel="predecessor-version",
                target=_publication_href(predecessor_href, record, output_item_path),
                media_type=pystac.MediaType.JSON,
                title=f"Superseded promotion {superseded_item.id}",
            )
        )
    item.add_asset(
        "scenario-promotion",
        _record_asset(
            record,
            output_item_path,
            title="Scenario promotion record",
            roles=["metadata", "decision"],
        ),
    )
    for reference in promotion.evidence:
        item.add_asset(reference.asset_key, _file_asset(reference, record, output_item_path))
    _declare_asset_extensions(item, list(promotion.evidence))
    return _publish_decision_item(
        catalog=catalog,
        item=item,
        collection_id=collection_id,
        collection_description=collection_description,
        expected_assets=PROMOTION_ITEM_ASSETS,
        license=license,
    )
