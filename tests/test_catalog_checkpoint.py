"""Exercise checkpoint adoption, archive verification, and failure recovery."""

import json
from pathlib import Path
import zipfile

import pytest

from stormhub import cli
from stormhub.met import catalog_checkpoint as adoption
from stormhub.met.catalog_population import resume_catalog
from test_catalog_population import add_events, catalog as catalog, snapshot


def legacy(catalog):
    """Create legacy statistics alongside stale rank-addressed products."""
    add_events(catalog)
    root = Path(catalog.get_self_href()).parent
    directory = root / "72hr-events"
    (directory / "storm-stats.csv").write_bytes(
        b"storm_date,min,mean,max,x,y\n2020-01-01T00,0,2,4,-92,32\n2020-01-01T06,1,3,5,-91,33\n"
    )
    (directory / "dss").mkdir()
    (directory / "dss/retained_target.dss").write_bytes(b"original DSS bytes")
    return root, directory


def apply(root, **kwargs):
    """Apply a stopped-writer plan with explicit operator attestations."""
    plan = adoption.adopt_checkpoint(root, duration_hours=72, dry_run=True)
    return adoption.adopt_checkpoint(
        root,
        duration_hours=72,
        settings_confirmed=True,
        writers_stopped=True,
        expected_checkpoint_sha256=plan["checkpoint_sha256"],
        **kwargs,
    )


def test_plan_is_read_only_and_missing_attestations_refuse(catalog):
    """A plan cannot migrate files or establish historical provenance on its own."""
    root, directory = legacy(catalog)
    before = snapshot(root)
    plan = adoption.adopt_checkpoint(root, duration_hours=72, dry_run=True)
    assert plan["completed_events"] == 2
    assert plan["file_count"] == len(plan["files"])
    with pytest.raises(ValueError, match="attestations"):
        adoption.adopt_checkpoint(root, duration_hours=72, expected_checkpoint_sha256=plan["checkpoint_sha256"])
    assert snapshot(root) == before


def test_adoption_preserves_all_original_bytes_and_enables_resume(catalog):
    """Archive every original file, retire stale Items, and resume from identical stats."""
    root, directory = legacy(catalog)
    before = snapshot(root)
    result = apply(root)
    backup = Path(result["backup"])
    assert result["status"] == "adopted"
    assert {path.name for path in directory.iterdir()} == {"storm-stats.csv", "population-settings.json"}
    assert (directory / "storm-stats.csv").read_bytes() == before[str(Path("72hr-events/storm-stats.csv"))][0]
    assert (backup / "retired/72hr-events/dss/retained_target.dss").read_bytes() == b"original DSS bytes"
    with zipfile.ZipFile(backup / "checkpoint.zip") as zipped:
        for name in result["files"]:
            assert zipped.read(name) == before[str(Path(name))][0]
    root_doc = json.loads((root / "catalog.json").read_bytes())
    assert not [link for link in root_doc["links"] if link["rel"] == "child"]
    plan = resume_catalog(root, duration_hours=72, num_workers=16, dry_run=True)
    assert plan["num_workers"] == 16
    receipt = json.loads((backup / "receipt.json").read_bytes())
    assert receipt["origin"] == "operator-confirmed-legacy-checkpoint"
    with pytest.raises(ValueError, match="already has"):
        adoption.adopt_checkpoint(root, duration_hours=72, dry_run=True)
    (backup / "receipt.json").write_text("{}")
    with pytest.raises(ValueError, match="receipt"):
        resume_catalog(root, duration_hours=72, dry_run=True)


@pytest.mark.parametrize(
    "row",
    [
        "2020-01-01T00,0,2,4,-92,32",  # duplicate
        "2020-01-01T01,0,2,4,-92,32",  # off the six-hour grid
        "2021-01-01T00,0,2,4,-92,32",  # out of range
        "2020-01-01T12,0,nan,4,-92,32",  # invalid values
        "2020-01-01T12,0,2",  # interrupted write
    ],
)
def test_invalid_statistics_refused_without_writes(catalog, row):
    """Do not silently drop malformed, duplicate, or scientifically incompatible rows."""
    root, directory = legacy(catalog)
    with (directory / "storm-stats.csv").open("a") as stream:
        stream.write(row + "\n")
    before = snapshot(root)
    with pytest.raises(ValueError):
        adoption.adopt_checkpoint(root, duration_hours=72, dry_run=True)
    assert snapshot(root) == before


def test_stale_plan_refuses_changed_checkpoint(catalog):
    """The reviewed fingerprint cannot authorize a different checkpoint."""
    root, directory = legacy(catalog)
    plan = adoption.adopt_checkpoint(root, duration_hours=72, dry_run=True)
    (directory / "dss/retained_target.dss").write_bytes(b"changed DSS bytes")
    before = snapshot(root)
    with pytest.raises(ValueError, match="fingerprint"):
        adoption.adopt_checkpoint(
            root,
            duration_hours=72,
            settings_confirmed=True,
            writers_stopped=True,
            expected_checkpoint_sha256=plan["checkpoint_sha256"],
        )
    assert snapshot(root) == before


def test_archive_failure_leaves_live_collection_untouched(catalog, monkeypatch):
    """Do not switch configurations or retire originals unless archive verification passes."""
    root, directory = legacy(catalog)
    before = snapshot(directory)
    catalog_bytes = (root / "catalog.json").read_bytes()

    def fail(*args):
        raise ValueError("archive verification failed")

    monkeypatch.setattr(adoption, "_verify_archive", fail)
    with pytest.raises(ValueError, match="archive verification"):
        apply(root)
    assert snapshot(directory) == before
    assert (root / "catalog.json").read_bytes() == catalog_bytes


def test_writer_change_during_backup_prevents_switch(catalog, monkeypatch):
    """Catch a legacy writer even after it initially appeared stopped."""
    root, directory = legacy(catalog)
    verify = adoption._verify_archive

    def concurrent_write(*args):
        verify(*args)
        with (directory / "storm-stats.csv").open("a") as stream:
            stream.write("2020-01-01T12,0,2,4,-92,32\n")

    monkeypatch.setattr(adoption, "_verify_archive", concurrent_write)
    with pytest.raises(ValueError, match="changed during backup"):
        apply(root)
    assert (directory / "dss/retained_target.dss").read_bytes() == b"original DSS bytes"
    assert not (directory / "population-settings.json").exists()


def test_post_switch_failure_rolls_back_originals(catalog, monkeypatch):
    """Restore the root and old collection if the new resume preflight fails."""
    root, directory = legacy(catalog)
    before = snapshot(directory)
    original_root = (root / "catalog.json").read_bytes()

    def fail(*args, **kwargs):
        raise RuntimeError("resume preflight failed")

    monkeypatch.setattr(adoption, "resume_catalog", fail)
    with pytest.raises(RuntimeError, match="preflight"):
        apply(root)
    assert snapshot(directory) == before
    assert (root / "catalog.json").read_bytes() == original_root
    assert (
        json.loads(next((root / "_checkpoint-adoptions").glob("*/transaction.json")).read_bytes())["state"]
        == "rolled-back"
    )


def test_cli_adoption_plan(catalog, capsys):
    """Expose the reviewable fingerprint through the installed command interface."""
    root, _ = legacy(catalog)
    assert cli.main(["adopt-checkpoint", str(root), "--duration", "72", "--dry-run"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["completed_events"] == 2 and len(plan["checkpoint_sha256"]) == 64
