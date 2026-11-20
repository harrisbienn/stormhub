"""Prove population accounting through the public CLI and real catalog finalizer."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pystac
import pytest

from stormhub import cli
from stormhub.met import catalog_population, storm_catalog
from stormhub.met.population_accounting import PopulationFailure
from stormhub.met.consts import TRANSPOSITION_METHOD
from test_catalog_population import catalog as catalog
from test_transpose_production_stride import historical_search


def configure(catalog):
    """Use a small frozen daily roster and actual threaded execution."""
    root = Path(catalog.get_self_href()).parent
    settings = json.loads((root / "creation-settings.json").read_bytes())
    settings["params"].update(use_threads=True, num_workers=2, check_every_n_hours=24, end_date="2020-01-10")
    (root / "creation-settings.json").write_text(json.dumps(settings), encoding="utf-8")
    return root


def worker(catalog, date, duration, *, return_item=False, por_rank=None, collection_id=None):
    """Stub only the external rainfall calculation, retaining the STAC and ranking paths."""
    stats = {"min": 0, "mean": 20 - date.day, "max": 25}
    stride = historical_search().selected_stride_cells if date.day == 1 else 4
    if not return_item:
        return {
            "storm_date": date.strftime("%Y-%m-%dT%H"),
            "aorc:statistics": stats,
            "translation_stride_cells": stride,
            "centroid": SimpleNamespace(x=0.5, y=0.5),
        }
    path = Path(catalog.spm.collection_item(collection_id, str(por_rank)))
    path.parent.mkdir()
    start = date.replace(tzinfo=timezone.utc)
    item = pystac.Item(
        str(por_rank),
        {"type": "Point", "coordinates": [0.5, 0.5]},
        [0.5] * 4,
        datetime=None,
        properties={"aorc:statistics": stats,
                    "stormhub:transposition_search": {"method": TRANSPOSITION_METHOD, "stride_cells": stride}},
        start_datetime=start,
        end_datetime=start + timedelta(hours=duration),
    )
    item.save_object(dest_href=str(path), include_self_link=False)
    return item


def attempts(root):
    """Load individual attempt evidence without relying on timestamps or UUID order."""
    return [json.loads(path.read_bytes()) for path in (root / "24hr-events/population-attempts").glob("*.json")]


@pytest.mark.parametrize("reason", ["OSError", "no_valid_placement"])
def test_missing_search_member_resume_and_ranked_identity(catalog, monkeypatch, reason):
    """One failed member cannot reach ranking; resume computes only the missing member."""
    root = configure(catalog)
    independent = root.parent.parent / "fresh" / root.name
    shutil.copytree(root, independent)
    calls = []

    def fail_one(catalog, date, duration, **options):
        if date.day == 4:
            if reason == "no_valid_placement":
                search = historical_search()
                search.data_array.values[:] = float("nan")
                search.max_transpose()
            raise OSError("injected member outage")
        return worker(catalog, date, duration, **options)

    monkeypatch.setattr(storm_catalog, "storm_search", fail_one)
    assert cli.main(["populate", str(root), "--duration", "24"]) == 1
    (first,) = attempts(root)
    assert first["status"] == "failed" and len(first["search"]["completed"]) == 9
    assert first["items"] is None and first["search"]["missing"] == ["2020-01-04T00"]
    assert first["worker_failures"] == {"2020-01-04T00:00:00": reason}
    assert first["search"]["fallback_dates"] == ["2020-01-01T00"]
    assert not (root / "24hr-events/collection.json").exists()
    record = (root / "24hr-events/population-settings.json").read_bytes()

    def recovered(catalog, date, duration, **options):
        if not options.get("return_item"):
            calls.append(date)
        return worker(catalog, date, duration, **options)

    monkeypatch.setattr(storm_catalog, "storm_search", recovered)
    assert cli.main(["resume", str(root), "--duration", "24"]) == 0
    assert calls == [datetime(2020, 1, 4)]
    assert (root / "24hr-events/population-settings.json").read_bytes() == record
    (successful,) = [attempt for attempt in attempts(root) if attempt["status"] == "complete"]
    assert successful["search"]["completed"] == json.loads(record)["requested_starts_utc"]
    assert successful["items"]["completed"] == ["1", "2"]
    assert successful["items"]["missing"] == []
    assert successful["search"]["fallback_dates"] == ["2020-01-01T00"]
    assert json.loads(record)["method"]["first_nonempty_strides_cells"] == [4, 1]
    assert first in attempts(root)
    assert cli.main(["populate", str(independent), "--duration", "24"]) == 0
    (fresh,) = attempts(independent)
    assert fresh["search"] == successful["search"] and fresh["items"] == successful["items"]
    for name in ("population-settings.json", "ranked-storms.csv"):
        assert (root / "24hr-events" / name).read_bytes() == (independent / "24hr-events" / name).read_bytes()


@pytest.mark.parametrize("failure", ["worker", "lost-item", "wrong-stride", "finalization"])
def test_incomplete_items_or_catalog_never_complete(catalog, monkeypatch, failure):
    """Require exact disk membership and a successful final catalog write."""
    root = configure(catalog)

    def interrupted(*args, **kwargs):
        if kwargs.get("por_rank") == 2 and failure == "worker":
            raise ValueError("injected Item failure")
        result = worker(*args, **kwargs)
        if kwargs.get("por_rank") == 2 and failure == "lost-item":
            (root / "24hr-events/2/2.json").unlink()
        if kwargs.get("por_rank") == 2 and failure == "wrong-stride":
            path = root / "24hr-events/2/2.json"
            item = json.loads(path.read_bytes())
            method = item["properties"]["stormhub:transposition_search"]
            method["stride_cells"] = 1 if method["stride_cells"] == 4 else 4
            path.write_text(json.dumps(item))
        return result

    def refuse_save(self):
        raise OSError("injected finalization failure")

    monkeypatch.setattr(storm_catalog, "storm_search", interrupted)
    if failure == "finalization":
        monkeypatch.setattr(storm_catalog.StormCatalog, "save_catalog", refuse_save)
    assert cli.main(["populate", str(root), "--duration", "24"]) == 1
    (attempt,) = attempts(root)
    assert attempt["status"] == "failed" and len(attempt["search"]["completed"]) == 10
    if failure == "finalization":
        assert attempt["phase"] == "finalization" and attempt["items"]["completed"] == ["1", "2"]
    else:
        assert attempt["items"]["missing"] == ["2"] and attempt["items"]["completed"] == ["1"]
    with pytest.raises(ValueError, match="existing Items"):
        catalog_population.resume_catalog(root, duration_hours=24)


def test_serial_failure_preserves_successful_rows(tmp_path):
    """Keep other results but propagate the failed member at the serial boundary."""

    def selected(catalog, date, duration):
        if date.day == 2:
            raise RuntimeError("injected")
        return worker(catalog, date, duration)

    stats = tmp_path / "storm-stats.csv"
    with pytest.raises(PopulationFailure) as error:
        storm_catalog.serial_processor(selected, None, 24, str(stats), [datetime(2020, 1, i) for i in (1, 2, 3)])
    assert error.value.failed == {"2020-01-02T00:00:00": "RuntimeError"}
    assert len(stats.read_text().splitlines()) == 3


def test_legacy_settings_refused_without_mutation(catalog):
    """Earlier records lacking the roster/method identity cannot acquire one by resuming."""
    root = configure(catalog)
    plan = catalog_population.populate_catalog(root, duration_hours=24, dry_run=True)
    legacy = {k: plan[k] for k in ("catalog_id", "inputs_sha256", "search")}
    legacy["version"] = 1
    directory = root / "24hr-events"
    directory.mkdir()
    path = directory / "population-settings.json"
    path.write_text(json.dumps(legacy))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="settings or geometry"):
        catalog_population.resume_catalog(root, duration_hours=24)
    assert path.read_bytes() == before and list(directory.iterdir()) == [path]


def failing_member(catalog, date, duration):
    """Run a picklable worker so the process-pool failure crosses a real child boundary."""
    if date.day == 2:
        raise RuntimeError("injected")
    return worker(catalog, date, duration)


def test_spawned_worker_failure(tmp_path):
    """Surface a failing process after retaining both successful statistics rows."""
    stats = tmp_path / "storm-stats.csv"
    with pytest.raises(PopulationFailure) as error:
        storm_catalog.multi_processor(
            failing_member, None, 24, str(stats), [datetime(2020, 1, i) for i in (1, 2, 3)], num_workers=2
        )
    assert error.value.failed == {"2020-01-02T00:00:00": "RuntimeError"}
    assert "2020-01-03T00" in stats.read_text()
