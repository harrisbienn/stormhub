"""Test shared population workflows with real STAC files and isolated compute."""

from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pystac
import pytest
from shapely.geometry import shape

from stormhub import cli
from stormhub.met import catalog_population as workflow
from stormhub.met import catalog_setup as setup
from stormhub.met import storm_catalog as storms


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    """Prepare a small real base catalog without accessing AORC."""
    settings = json.loads((Path(__file__).parents[1] / "configs/params-config.json").read_text())
    settings["catalog_id"] = "test-catalog"
    settings["params"].update(start_date="2020-01-01", end_date="2020-01-03", top_n_events=2)
    polygon = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}
    for key in ("watershed", "transposition_region"):
        file = tmp_path / f"{key}.json"
        file.write_text(
            json.dumps(
                {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": polygon, "properties": {}}]}
            )
        )
        settings[key].update(geometry_file=file.name, source_sha256=setup.sha256_file(file))
    config = setup.prepare_catalog_inputs(settings, tmp_path, tmp_path / "catalogs")
    monkeypatch.setattr(storms, "valid_spaces_item", lambda *args: shape(polygon))
    return setup.create_prepared_catalog(config)


def snapshot(root):
    """Detect any content or timestamp writes during planning and refusal."""
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }


def add_events(catalog, duration=72):
    """Persist a two-Item collection using real relative STAC links."""
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    end = datetime(2020, 1, 4, tzinfo=timezone.utc)
    collection = pystac.Collection(
        id=f"{duration}hr-events",
        description="test storms",
        license="proprietary",
        extent=pystac.Extent(pystac.SpatialExtent([[0, 0, 1, 1]]), pystac.TemporalExtent([[start, end]])),
    )
    catalog.add_child(collection)
    for item_id in ("1", "2"):
        collection.add_item(
            pystac.Item(
                item_id,
                {"type": "Point", "coordinates": [0.5, 0.5]},
                [0.5, 0.5, 0.5, 0.5],
                datetime=None,
                properties={},
                start_datetime=start,
                end_datetime=end,
            )
        )
    collection.normalize_hrefs(str(Path(catalog.get_self_href()).parent / collection.id))
    collection.save(catalog_type=pystac.CatalogType.SELF_CONTAINED)
    catalog.save_object(include_self_link=False)
    return collection


def interrupt_search(catalog, monkeypatch):
    """Simulate an interrupted native search after its first statistics write."""

    def interrupt(path, **options):
        target = Path(path).parent / "72hr-events/storm-stats.csv"
        target.write_text("storm_date,value\n2020-01-01T00,1\n")
        raise OSError("search interrupted")

    monkeypatch.setattr(storms, "new_collection", interrupt)
    with pytest.raises(OSError, match="interrupted"):
        workflow.populate_catalog(catalog.get_self_href())


def test_population_plan_is_read_only_and_uses_frozen_settings(catalog):
    """Resolve the saved snapshot, independent of any editable repository config."""
    path = Path(catalog.get_self_href())
    before = snapshot(path.parent)
    plan = workflow.populate_catalog(path, duration_hours=48, num_workers=3, dry_run=True)
    assert plan["search"]["storm_duration"] == 48
    assert plan["search"]["start_date"] == "2020-01-01"
    assert plan["search"]["check_every_n_hours"] == 6
    assert plan["num_workers"] == 3 and plan["use_threads"] is False
    assert snapshot(path.parent) == before


def test_smoke_population_records_actual_scope_and_refuses_second_run(catalog, monkeypatch):
    """Persist smoke provenance before compute and never silently reuse its outputs."""
    path = Path(catalog.get_self_href())
    seen = {}

    def compute(catalog_path, **options):
        record = json.loads((path.parent / "72hr-events/population-settings.json").read_text())
        seen.update(options=options, record=record)
        return SimpleNamespace(id="72hr-events")

    monkeypatch.setattr(storms, "new_collection", compute)
    dates = [datetime(2020, 1, 1, tzinfo=timezone.utc)]
    workflow.populate_catalog(path, specific_dates=dates)
    assert seen["options"]["specific_dates"] == [datetime(2020, 1, 1)]
    assert seen["record"]["search"]["specific_dates"] == ["2020-01-01T00:00:00"]
    before = snapshot(path.parent)
    with pytest.raises(FileExistsError, match="workspace exists"):
        workflow.populate_catalog(path)
    with pytest.raises(ValueError, match="smoke search"):
        workflow.resume_catalog(path)
    assert snapshot(path.parent) == before


def test_resume_same_search_and_worker_override(catalog, monkeypatch):
    """Resume with changed worker count while retaining result-defining settings."""
    interrupt_search(catalog, monkeypatch)
    seen = {}

    def resume(path, **options):
        seen.update(options)
        return SimpleNamespace(id="72hr-events")

    monkeypatch.setattr(storms, "resume_collection", resume)
    assert workflow.resume_catalog(catalog.get_self_href(), num_workers=2).id == "72hr-events"
    assert seen["num_workers"] == 2
    assert seen["storm_duration"] == 72
    assert seen["top_n_events"] == 2


@pytest.mark.parametrize("change", ["settings", "geometry", "items", "missing-record"])
def test_resume_refuses_unsafe_state_without_writes(catalog, monkeypatch, change):
    """Reject changed identity, rank-addressed products, and undocumented searches."""
    interrupt_search(catalog, monkeypatch)
    root = Path(catalog.get_self_href()).parent
    if change == "settings":
        path = root / "creation-settings.json"
        settings = json.loads(path.read_text())
        settings["params"]["top_n_events"] = 10
        path.write_text(json.dumps(settings))
    elif change == "geometry":
        path = next((root / "hydro_domains").glob("*.json"))
        path.write_text(path.read_text() + " ")
    elif change == "items":
        (root / "72hr-events/1").mkdir()
    else:
        (root / "72hr-events/population-settings.json").unlink()
    before = snapshot(root)
    with pytest.raises(ValueError):
        workflow.resume_catalog(root, dry_run=True)
    assert snapshot(root) == before


@pytest.mark.parametrize(
    "key,value",
    [
        ("num_workers", 0),
        ("top_n_events", True),
        ("min_precip_threshold_inches", float("nan")),
        ("use_threads", "false"),
        ("end_date", "bad"),
    ],
)
def test_bad_settings_fail_before_creating_outputs(catalog, key, value):
    """Report invalid operational settings before starting a costly search."""
    root = Path(catalog.get_self_href()).parent
    file = root / "creation-settings.json"
    settings = json.loads(file.read_text())
    settings["params"][key] = value
    file.write_text(json.dumps(settings))
    with pytest.raises(ValueError):
        workflow.populate_catalog(root)
    assert not (root / "72hr-events").exists()


def test_empty_smoke_dates_cannot_trigger_full_search(catalog):
    """An empty explicit selection must never expand to the full date range."""
    with pytest.raises(ValueError, match="must not be empty"):
        workflow.populate_catalog(catalog.get_self_href(), specific_dates=[])


def test_no_collection_is_reported_as_failure(catalog, monkeypatch):
    """Do not report success when the scientific API found no usable collection."""
    monkeypatch.setattr(storms, "new_collection", lambda *args, **kwargs: None)
    with pytest.raises(RuntimeError, match="no collection"):
        workflow.populate_catalog(catalog.get_self_href())


def test_export_selection_overwrite_and_frozen_defaults(catalog, monkeypatch):
    """Preserve existing products by default and export only explicit selections."""
    collection = add_events(catalog)
    root = Path(catalog.get_self_href()).parent
    dss = root / "72hr-events/dss"
    dss.mkdir()
    filename = storms.storm_dss_filename(collection.get_item("1"), output_resolution_km=1, spatial_role="target")
    (dss / filename).write_bytes(b"keep this product")
    before = snapshot(root)
    with pytest.raises(FileExistsError, match="already exist"):
        workflow.export_catalog_dss(root, item_ids=["1"], output_modes=["target"])
    plan = workflow.export_catalog_dss(root, item_ids=["1"], output_modes=["target"], overwrite=True, dry_run=True)
    assert plan["existing_outputs"] == [str(dss / filename)]
    assert snapshot(root) == before
    seen = {}

    def export(loaded, **options):
        seen.update(options)
        return {"failed_count": 0}

    monkeypatch.setattr(storms, "add_storm_dss_files", export)
    workflow.export_catalog_dss(root, item_ids=["2"])
    assert seen["item_ids"] == ["2"]
    assert seen["output_modes"] == ("source", "target")
    assert seen["collection_id"] == "72hr-events"
    assert seen["target_buffer_km"] == 5
    assert seen["use_valid_region"] is True
    assert snapshot(root) == before


@pytest.mark.parametrize(
    "options", [{}, {"item_ids": []}, {"item_ids": ["1"], "all_items": True}, {"item_ids": ["missing"]}]
)
def test_export_requires_valid_explicit_selection(catalog, options):
    """Avoid implicit all-Item exports and silently ignored unknown IDs."""
    add_events(catalog)
    with pytest.raises(ValueError):
        workflow.export_catalog_dss(catalog.get_self_href(), **options)


def test_all_items_and_duration_validation(catalog):
    """Whole-collection selection is explicit and must match Item time windows."""
    collection = add_events(catalog)
    plan = workflow.export_catalog_dss(catalog.get_self_href(), all_items=True, dry_run=True)
    assert plan["item_ids"] == ["1", "2"]
    item = collection.get_item("2")
    item.properties["end_datetime"] = "2020-01-03T00:00:00Z"
    item.save_object()
    with pytest.raises(ValueError, match="does not match the selected duration"):
        workflow.export_catalog_dss(catalog.get_self_href(), all_items=True, dry_run=True)


def test_mismatched_snapshot_id_is_rejected(catalog):
    """A copied snapshot cannot redirect operations to a different generation."""
    root = Path(catalog.get_self_href()).parent
    path = root / "creation-settings.json"
    settings = json.loads(path.read_text())
    settings["catalog_id"] = "another-catalog"
    path.write_text(json.dumps(settings))
    before = snapshot(root)
    with pytest.raises(ValueError, match="must agree"):
        workflow.populate_catalog(root)
    assert snapshot(root) == before


def test_cli_errors_and_partial_exports_return_nonzero(catalog, monkeypatch, capsys):
    """Expose partial failures to shell automation while retaining the run summary."""
    monkeypatch.setattr(
        workflow, "export_catalog_dss", lambda *args, **kwargs: {"failed_count": 1, "status": "partial"}
    )
    assert cli.main(["export-dss", catalog.get_self_href(), "--duration", "72", "--all-items"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "partial"
    assert cli.main(["populate", catalog.get_self_href(), "--duration", "0"]) == 1
    with pytest.raises(SystemExit) as exc:
        cli.main(["export-dss", catalog.get_self_href(), "--duration", "72"])
    assert exc.value.code == 2


def test_cli_populate_dry_run(catalog, capsys):
    """Resolve repeatable exact dates and worker overrides through the parser."""
    assert (
        cli.main(
            [
                "populate",
                catalog.get_self_href(),
                "--duration",
                "24",
                "--workers",
                "2",
                "--specific-date",
                "2020-01-01T06:00:00+00:00",
                "--dry-run",
            ]
        )
        == 0
    )
    plan = json.loads(capsys.readouterr().out)
    assert plan["search"]["specific_dates"] == ["2020-01-01T06:00:00"]
    assert plan["num_workers"] == 2


def test_low_level_resume_returns_collection_and_passes_empty_selection(catalog, monkeypatch):
    """Keep an already complete statistics search from expanding to all dates."""
    monkeypatch.setattr(storms, "find_missing_storm_dates", lambda *args, **kwargs: [])
    seen = {}
    expected = SimpleNamespace(id="72hr-events")

    def collect(**options):
        seen.update(options)
        return expected

    monkeypatch.setattr(storms, "new_collection", collect)
    assert storms.resume_collection(catalog.get_self_href()) is expected
    assert seen["specific_dates"] == []


def test_new_collection_empty_dates_does_not_collect(catalog, monkeypatch):
    """Continue ranking existing statistics without a new AORC collection pass."""

    def forbidden(*args, **kwargs):
        pytest.fail("Empty specific_dates unexpectedly started discovery")

    def stop_at_analysis(*args, **kwargs):
        raise ValueError("No events")

    monkeypatch.setattr(storms, "generate_date_range", forbidden)
    monkeypatch.setattr(storms, "collect_event_stats", forbidden)
    monkeypatch.setattr(storms, "StormAnalyzer", stop_at_analysis)
    assert storms.new_collection(catalog, specific_dates=[]) is None
