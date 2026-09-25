"""Archive and adopt a stopped legacy search without reusing ranked products."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
from uuid import uuid4
import zipfile

from stormhub.met.catalog_population import PopulationContext, resume_catalog


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha(path: Path) -> str:
    with path.open("rb") as stream:
        return _stream_sha(stream)


def _stream_sha(stream) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _inventory(context: PopulationContext) -> dict:
    root = context.catalog_file.parent
    pending = [context.directory]
    paths = [context.catalog_file]
    paths += [root / name for name in context.provenance(context.search())["inputs_sha256"]]
    while pending:
        path = pending.pop()
        if path.resolve() != path or not path.is_relative_to(root):
            raise ValueError(f"Checkpoint paths must be local and cannot traverse links: {path}")
        if path.is_dir():
            pending.extend(path.iterdir())
        elif path.is_file():
            paths.append(path)
        else:
            raise ValueError(f"Unexpected checkpoint entry: {path}")
    result = {}
    for path in sorted(paths):
        before = path.stat()
        digest = _sha(path)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("Checkpoint is changing; stop its writers before adoption")
        result[path.relative_to(root).as_posix()] = {"bytes": after.st_size, "sha256": digest}
    return result


def _validate_stats(payload: bytes, search: dict) -> int:
    try:
        reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig"), newline=""), strict=True)
        if reader.fieldnames != ["storm_date", "min", "mean", "max", "x", "y"]:
            raise ValueError("Checkpoint must have storm_date,min,mean,max,x,y columns")
        start = datetime.strptime(search["start_date"], "%Y-%m-%d")
        end = datetime.strptime(search["end_date"], "%Y-%m-%d")
        interval = search["check_every_n_hours"] * 3600
        seen = set()
        for line, row in enumerate(reader, 2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"Incomplete checkpoint row {line}; preserve and repair before adoption")
            date = datetime.strptime(row["storm_date"], "%Y-%m-%dT%H")
            if date in seen or not start <= date <= end or (date - start).total_seconds() % interval:
                raise ValueError(f"Duplicate or out-of-scope checkpoint date at row {line}: {row['storm_date']}")
            values = [float(row[key]) for key in ("min", "mean", "max", "x", "y")]
            if not all(math.isfinite(value) for value in values) or not 0 <= values[0] <= values[1] <= values[2]:
                raise ValueError(f"Invalid checkpoint statistics at row {line}")
            seen.add(date)
    except (UnicodeError, csv.Error) as exc:
        raise ValueError("Checkpoint CSV is malformed; preserve and inspect it before adoption") from exc
    if not seen:
        raise ValueError("Checkpoint has no completed events to adopt")
    return len(seen)


def _unlinked_catalog(context: PopulationContext) -> bytes:
    document = json.loads(context.catalog_file.read_bytes())
    links = []
    for link in document.get("links", []):
        href = link.get("href", "")
        # Only the exact selected collection's child link may be retired.
        if "://" not in href:
            target = (context.catalog_file.parent / href).resolve()
            if target.is_relative_to(context.directory):
                if link.get("rel") != "child" or target != context.directory / "collection.json":
                    raise ValueError("Unexpected root link into the checkpoint; inspect it before adoption")
                continue
        links.append(link)
    document["links"] = links
    return _json_bytes(document)


def _verify_archive(archive: Path, inventory: dict) -> None:
    with zipfile.ZipFile(archive) as zipped:
        if len(zipped.namelist()) != len(inventory) or set(zipped.namelist()) != set(inventory):
            raise ValueError("Checkpoint archive inventory does not match its source")
        for name, entry in inventory.items():
            with zipped.open(name) as stream:
                if _stream_sha(stream) != entry["sha256"] or zipped.getinfo(name).file_size != entry["bytes"]:
                    raise ValueError(f"Checkpoint archive verification failed: {name}")


def _relocate(source: Path, destination: Path, root: Path) -> None:
    for path in (source, destination):
        if path.resolve() != path or not path.is_relative_to(root):
            raise ValueError(f"Unsafe adoption move target: {path}")
    if destination.exists():
        raise FileExistsError(f"Adoption move destination already exists: {destination}")
    source.rename(destination)


def adopt_checkpoint(
    catalog: str | Path,
    *,
    duration_hours: int,
    settings_confirmed: bool = False,
    writers_stopped: bool = False,
    expected_checkpoint_sha256: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Adopt operator-confirmed legacy statistics after a verified full backup.

    CSV dates/values are checked, but historical geometry and kernel settings
    cannot be inferred from CSV alone. ``settings_confirmed`` attests that all
    retained rows used the displayed duration and domain inputs. A stopped smoke
    checkpoint can seed a full search when its rows meet that same contract.

    Execution requires the fingerprint from a stopped-writer dry run. Source
    files are hashed again before switching. The whole old duration directory
    remains under a unique backup path, together with a verified ZIP and the
    original root. Caught switch failures roll back; abrupt process/host failure
    leaves transaction files for manual recovery. This is not a writer lock.
    """
    context = PopulationContext.load(catalog, duration_hours)
    if not context.directory.is_dir():
        raise ValueError("No duration checkpoint directory exists")
    if (context.directory / "population-settings.json").exists():
        raise ValueError("Checkpoint already has population provenance; use resume or inspect that record")
    provenance = context.provenance(context.search())
    inventory = _inventory(context)
    stats_name = f"{context.collection_id}/storm-stats.csv"
    stats = (context.directory / "storm-stats.csv").read_bytes()
    if hashlib.sha256(stats).hexdigest() != inventory[stats_name]["sha256"]:
        raise ValueError("Checkpoint changed while reading statistics; stop its writers")
    completed = _validate_stats(stats, provenance["search"])
    root_after = _unlinked_catalog(context)
    fingerprint = hashlib.sha256(_json_bytes({"files": inventory, "provenance": provenance})).hexdigest()
    plan = {
        "operation": "adopt-checkpoint",
        "catalog": str(context.catalog_file),
        "collection_id": context.collection_id,
        "completed_events": completed,
        "checkpoint_sha256": fingerprint,
        "file_count": len(inventory),
        "total_bytes": sum(entry["bytes"] for entry in inventory.values()),
        "provenance": provenance,
        "files": inventory,
        "action": "Archive and retire the old collection; retain only statistics and adoption provenance for resume",
    }
    if _inventory(context) != inventory:
        raise ValueError("Checkpoint changed during inspection; stop its writers and rerun the plan")
    if dry_run:
        return plan
    if not settings_confirmed or not writers_stopped:
        raise ValueError("Adoption requires settings_confirmed and writers_stopped operator attestations")
    if expected_checkpoint_sha256 != fingerprint:
        raise ValueError("Checkpoint fingerprint differs or is missing; review a new stopped-writer dry run")

    root = context.catalog_file.parent
    backup = root / "_checkpoint-adoptions" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8])
    if backup.resolve() != backup or not backup.is_relative_to(root):
        raise ValueError("Adoption backup must remain inside the catalog without filesystem links")
    backup.mkdir(parents=True, exist_ok=False)
    (backup / "source-manifest.json").write_bytes(_json_bytes(plan))
    archive = backup / "checkpoint.zip"
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zipped:
        for name in inventory:
            zipped.write(root / name, name)
    _verify_archive(archive, inventory)
    archive_sha = _sha(archive)
    (backup / "checkpoint.zip.sha256").write_text(f"{archive_sha}  checkpoint.zip\n", encoding="utf-8")
    if _inventory(context) != inventory:
        raise ValueError(f"Checkpoint changed during backup; original remains active. Inspect {backup}")

    receipt = {
        "version": 1,
        "origin": "operator-confirmed-legacy-checkpoint",
        "adopted_at": datetime.now(timezone.utc).isoformat(),
        "checkpoint_sha256": fingerprint,
        "settings_confirmed": True,
        "writers_stopped": True,
        "completed_events": completed,
        "archive_sha256": archive_sha,
        "provenance": provenance,
        "statistics_sha256": inventory[stats_name]["sha256"],
    }
    receipt_path = backup / "receipt.json"
    receipt_path.write_bytes(_json_bytes(receipt))
    record = {
        **provenance,
        "adoption": {"receipt": receipt_path.relative_to(root).as_posix(), "sha256": _sha(receipt_path)},
    }
    staged = backup / "prepared" / context.collection_id
    staged.mkdir(parents=True)
    (staged / "storm-stats.csv").write_bytes(stats)
    (staged / "population-settings.json").write_bytes(_json_bytes(record))
    shutil.copyfile(context.catalog_file, backup / "catalog.before.json")
    (backup / "catalog.after.json").write_bytes(root_after)
    retired = backup / "retired" / context.collection_id
    retired.parent.mkdir()
    # Verify the final resolved move targets and every input immediately before switching.
    for path in (context.directory, staged, retired):
        if path.resolve() != path or not path.is_relative_to(root):
            raise ValueError(f"Unsafe adoption move target: {path}")
    if _inventory(context) != inventory:
        raise ValueError("Checkpoint changed before switching; original remains active")
    switched_old = switched_new = switched_root = False
    try:
        (backup / "transaction.json").write_bytes(
            _json_bytes({"state": "switching", "collection_id": context.collection_id})
        )
        _relocate(context.directory, retired, root)
        switched_old = True
        _relocate(staged, context.directory, root)
        switched_new = True
        shutil.copyfile(backup / "catalog.after.json", backup / "catalog.switch.json")
        os.replace(backup / "catalog.switch.json", context.catalog_file)
        switched_root = True
        # Validate the new live workspace using the exact same resume preflight.
        resume_catalog(context.catalog_file, duration_hours=duration_hours, dry_run=True)
        (backup / "transaction.json").write_bytes(
            _json_bytes({"state": "complete", "collection_id": context.collection_id})
        )
    except BaseException:
        # Deliberate transaction boundary: include KeyboardInterrupt and preserve all files.
        if switched_new:
            _relocate(context.directory, backup / "failed-prepared", root)
        if switched_old:
            _relocate(retired, context.directory, root)
        if switched_root:
            shutil.copyfile(backup / "catalog.before.json", backup / "catalog.restore.json")
            os.replace(backup / "catalog.restore.json", context.catalog_file)
        (backup / "transaction.json").write_bytes(
            _json_bytes({"state": "rolled-back", "collection_id": context.collection_id})
        )
        raise
    return {**plan, "status": "adopted", "backup": str(backup), "archive_sha256": archive_sha}
