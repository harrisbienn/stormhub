"""Shared, explicit population and DSS workflows for notebooks and the CLI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Sequence

import pystac

from stormhub.met.catalog_setup import load_catalog_settings
from stormhub.met import storm_catalog as storms


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _number(value: object, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if value < 0 or (positive and value == 0):
        raise ValueError(f"{name} must be {'positive' if positive else 'nonnegative'}")
    return value


def _utc_date(value: str | datetime) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if not isinstance(result, datetime):
        raise ValueError("Dates must be ISO timestamps or datetime objects")
    if result.tzinfo is not None:
        result = result.astimezone(timezone.utc).replace(tzinfo=None)
    if result.minute or result.second or result.microsecond:
        raise ValueError("Storm dates must be aligned to whole UTC hours")
    return result


@dataclass
class PopulationContext:
    """Resolve one local catalog and its frozen settings without network access."""

    catalog_file: Path
    settings: dict[str, Any]
    duration_hours: int

    @property
    def directory(self) -> Path:
        """Return the selected duration's output directory."""
        return self.catalog_file.parent / self.collection_id

    @property
    def collection_id(self) -> str:
        """Return the package's duration-based collection ID."""
        return f"{self.duration_hours}hr-events"

    @classmethod
    def load(cls, catalog: str | Path, duration_hours: int | None = None) -> PopulationContext:
        """Require a local catalog whose identity matches creation-settings.json."""
        path = Path(catalog).absolute()
        if path.is_dir():
            path /= "catalog.json"
        if path.resolve() != path:
            raise ValueError("Catalog paths must not traverse symlinks or junctions")
        document = json.loads(path.read_text(encoding="utf-8"))
        settings = load_catalog_settings(path.parent / "creation-settings.json")
        if document.get("id") != settings["catalog_id"] or path.parent.name != settings["catalog_id"]:
            raise ValueError("Catalog ID, directory, and creation-settings.json must agree")
        for title, domain_id in (
            ("Watershed", settings["watershed"]["id"]),
            ("Transposition Region", settings["transposition_region"]["id"]),
            ("Valid Transposition Region", settings["transposition_region"]["id"] + "_valid"),
        ):
            links = [link for link in document.get("links", []) if link.get("title") == title]
            domain = path.parent / "hydro_domains" / f"{domain_id}.json"
            if len(links) != 1 or (path.parent / links[0].get("href", "")).resolve() != domain or not domain.is_file():
                raise ValueError(f"Catalog must link to its local {title} Item")
        params = settings.get("params")
        if not isinstance(params, dict):
            raise ValueError("creation-settings.json must contain a params object")
        duration = duration_hours if duration_hours is not None else params.get("storm_duration_hours")
        context = cls(path, settings, _positive_int(duration, "duration_hours"))
        if context.directory.resolve() != context.directory:
            raise ValueError("Collection directory must not traverse symlinks or junctions")
        return context

    def search(self, specific_dates: Sequence[datetime] | None = None) -> dict[str, Any]:
        """Validate result-defining search options from the frozen snapshot."""
        params = self.settings["params"]
        for key in ("start_date", "end_date"):
            if not isinstance(params.get(key), str):
                raise ValueError(f"params.{key} must be an ISO date")
        start, end = (datetime.strptime(params[key], "%Y-%m-%d") for key in ("start_date", "end_date"))
        if start > end:
            raise ValueError("start_date must be on or before end_date")
        dates = None
        if specific_dates is not None:
            dates = sorted({_utc_date(value).isoformat() for value in specific_dates})
            if not dates:
                raise ValueError("specific_dates must not be empty; omit it for the full search")
        return {
            "start_date": params["start_date"],
            "end_date": params["end_date"],
            "storm_duration": self.duration_hours,
            "min_precip_threshold": _number(params.get("min_precip_threshold_inches"), "min_precip_threshold_inches"),
            "top_n_events": _positive_int(params.get("top_n_events"), "top_n_events"),
            "check_every_n_hours": _positive_int(params.get("check_every_n_hours"), "check_every_n_hours"),
            "specific_dates": dates,
        }

    def provenance(self, search: dict[str, Any]) -> dict[str, Any]:
        """Bind resume to the snapshot and catalog domain bytes used at discovery."""
        root = self.catalog_file.parent
        names = ["creation-settings.json"]
        names += [f"hydro_domains/{self.settings[key]['id']}.json" for key in ("watershed", "transposition_region")]
        names.append(f"hydro_domains/{self.settings['transposition_region']['id']}_valid.json")
        hashes = {}
        for name in names:
            path = root / name
            if path.resolve() != path:
                raise ValueError(f"Population input must not traverse filesystem links: {path}")
            hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        return {"version": 1, "catalog_id": self.settings["catalog_id"], "inputs_sha256": hashes, "search": search}


def _workers(context: PopulationContext, num_workers: int | None) -> dict[str, Any]:
    params = context.settings["params"]
    count = params.get("num_workers") if num_workers is None else num_workers
    threads = params.get("use_threads")
    if type(threads) is not bool:
        raise ValueError("use_threads must be a boolean")
    return {"num_workers": _positive_int(count, "num_workers"), "use_threads": threads}


def populate_catalog(
    catalog: str | Path,
    *,
    duration_hours: int | None = None,
    num_workers: int | None = None,
    specific_dates: Sequence[datetime] | None = None,
    with_tb: bool = False,
    dry_run: bool = False,
) -> pystac.Collection | dict[str, Any]:
    """Start a fresh collection, refusing any existing duration workspace.

    Record search provenance before calling the scientific API. A dry run only
    validates inputs and returns the resolved plan; it creates no files.
    """
    context = PopulationContext.load(catalog, duration_hours)
    search = context.search(specific_dates)
    record = context.provenance(search)
    workers = _workers(context, num_workers)
    if context.directory.exists():
        raise FileExistsError(
            f"Collection workspace exists: {context.directory}. Use resume for a recorded interrupted search; "
            "use a new catalog for a replacement or full search after a smoke run."
        )
    plan = {"operation": "populate", "catalog": str(context.catalog_file), **record, **workers}
    if dry_run:
        return plan
    context.directory.mkdir(exist_ok=False)
    with (context.directory / "population-settings.json").open("x", encoding="utf-8") as stream:
        json.dump(record, stream, indent=2)
        stream.write("\n")
    dates = None if search["specific_dates"] is None else [_utc_date(value) for value in search["specific_dates"]]
    result = storms.new_collection(
        str(context.catalog_file),
        **{**search, "specific_dates": dates},
        **workers,
        with_tb=with_tb,
    )
    if result is None:
        raise RuntimeError("Population produced no collection; inspect logs and storm statistics before retrying")
    return result


def resume_catalog(
    catalog: str | Path,
    *,
    duration_hours: int | None = None,
    num_workers: int | None = None,
    with_tb: bool = False,
    dry_run: bool = False,
) -> pystac.Collection | dict[str, Any]:
    """Resume a recorded full search before rank-addressed Items are written.

    Refuse unrecorded/changed inputs, smoke searches, and existing Items or DSS:
    reranking those could assign existing products to a different event.
    """
    context = PopulationContext.load(catalog, duration_hours)
    search = context.search()
    expected = context.provenance(search)
    record_path = context.directory / "population-settings.json"
    if not record_path.is_file():
        raise ValueError("Resume requires population-settings.json from populate; legacy searches cannot be adopted")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    if record != expected:
        raise ValueError("Resume settings or geometry differ, or this was a smoke search; use a new catalog")
    allowed = {"population-settings.json", "storm-stats.csv", "ranked-storms.csv"}
    if any(
        path.name not in allowed or not path.is_file() or path.resolve() != path for path in context.directory.iterdir()
    ):
        raise ValueError(
            "Resume refuses existing Items, DSS, or other products; inspect and reconcile the interrupted run"
        )
    if not (context.directory / "storm-stats.csv").is_file():
        raise ValueError("Resume requires partial storm-stats.csv; no search checkpoint is available")
    workers = _workers(context, num_workers)
    if dry_run:
        return {"operation": "resume", "catalog": str(context.catalog_file), **expected, **workers}
    result = storms.resume_collection(
        str(context.catalog_file),
        **{key: value for key, value in search.items() if key != "specific_dates"},
        **workers,
        with_tb=with_tb,
    )
    if result is None:
        raise RuntimeError("Resume produced no collection; inspect logs and storm statistics")
    return result


def export_catalog_dss(
    catalog: str | Path,
    *,
    duration_hours: int | None = None,
    item_ids: Sequence[str] | None = None,
    all_items: bool = False,
    output_modes: Sequence[str] | None = None,
    overwrite: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Export explicitly selected Items; refuse existing outputs unless opted in.

    ``overwrite`` applies only to selected Items and modes. It is not atomic:
    the underlying exporter replaces those DSS files. Back them up first.
    Return the package's per-item result, including partial failures.
    """
    if (all_items and item_ids is not None) or (not all_items and not item_ids):
        raise ValueError("Select either nonempty item_ids or all_items=True")
    context = PopulationContext.load(catalog, duration_hours)
    params = context.settings["params"]
    modes = tuple(dict.fromkeys(params.get("dss_output_modes", ()) if output_modes is None else output_modes))
    if not modes or set(modes) - {"source", "target"}:
        raise ValueError("output_modes must contain source and/or target")
    resolution = _number(params.get("output_resolution_km"), "output_resolution_km", positive=True)
    buffer = _number(params.get("target_buffer_km"), "target_buffer_km")
    valid = params.get("use_valid_region")
    if type(valid) is not bool:
        raise ValueError("use_valid_region must be a boolean")
    loaded = storms.StormCatalog.from_file(str(context.catalog_file))
    collection = storms.get_events_collection(loaded, collection_id=context.collection_id)
    if Path(collection.get_self_href()).resolve() != context.directory / "collection.json":
        raise ValueError("Selected collection must be stored in its local duration directory")
    available = {item.id: item for item in collection.get_items()}
    selected = list(available) if all_items else list(dict.fromkeys(str(value) for value in item_ids))
    if not selected or set(selected) - available.keys():
        raise ValueError(f"Select existing Items in {context.collection_id}; available IDs: {sorted(available)}")
    existing = []
    for item_id in selected:
        item = available[item_id]
        if (
            not item_id.isdecimal()
            or Path(item.get_self_href()).resolve() != context.directory / item_id / f"{item_id}.json"
        ):
            raise ValueError(f"Storm Item {item_id!r} must use a local numeric rank directory")
        start = _utc_date(item.properties["start_datetime"])
        end = _utc_date(item.properties["end_datetime"])
        if (end - start).total_seconds() != context.duration_hours * 3600:
            raise ValueError(f"Storm Item {item_id!r} does not match the selected duration")
        for mode in modes:
            path = (
                context.directory
                / "dss"
                / storms.storm_dss_filename(
                    item,
                    output_resolution_km=resolution,
                    spatial_role=mode,
                )
            )
            if path.resolve() != path:
                raise ValueError(f"DSS destination must not traverse filesystem links: {path}")
            if path.exists() or f"dss-{mode}" in item.assets:
                existing.append(str(path))
    if existing and not overwrite:
        raise FileExistsError(
            f"Selected DSS outputs already exist; choose unexported Items or explicit overwrite: {existing}"
        )
    options = {
        "collection_id": context.collection_id,
        "aoi_name": loaded.id,
        "use_valid_region": valid,
        "dss_output_dir": str(context.directory / "dss"),
        "output_resolution_km": resolution,
        "output_modes": modes,
        "target_buffer_km": buffer,
        "item_ids": selected,
    }
    if dry_run:
        return {
            "operation": "export-dss",
            "catalog": str(context.catalog_file),
            "duration_hours": context.duration_hours,
            "overwrite": overwrite,
            "existing_outputs": existing,
            **options,
        }
    return storms.add_storm_dss_files(
        loaded,
        variable_duration_map={storms.NOAADataVariable.APCP: context.duration_hours},
        **options,
    )
