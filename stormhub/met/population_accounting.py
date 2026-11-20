"""Record the requested and observed members of a frozen catalog search."""

from __future__ import annotations

from contextlib import contextmanager
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from uuid import uuid4

from stormhub._files import atomic_write_bytes
from stormhub.met.consts import STATS_HEADER, TRANSLATION_STRIDES, TRANSPOSITION_METHOD


class PopulationFailure(RuntimeError):
    """One or more requested workers failed after other members were allowed to finish."""

    def __init__(self, stage: str, failed: dict[str, str]):
        self.failed = failed
        super().__init__(f"{stage}: {len(failed)} failed member(s), first: {next(iter(failed))}")


def _read_placements(path: Path, *, adopted: bool) -> dict[str, int | None]:
    """Read and validate the actual spacing for every checkpoint row."""
    seen = {}
    if not path.exists():
        return seen
    valid_spacings = {str(stride) for stride in TRANSLATION_STRIDES}
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        if reader.fieldnames != STATS_HEADER.split(","):
            raise ValueError("Unexpected storm-stats.csv columns")
        for row in reader:
            if None in row or None in row.values():
                raise ValueError(f"Incomplete checkpoint row {reader.line_num}")
            date = row["storm_date"]
            values = [float(row[key]) for key in ("min", "mean", "max", "x", "y")]
            if (
                date in seen
                or datetime.strptime(date, "%Y-%m-%dT%H").strftime("%Y-%m-%dT%H") != date
                or not all(math.isfinite(value) for value in values)
                or not values[0] <= values[1] <= values[2]
            ):
                raise ValueError(f"Duplicate date or invalid checkpoint statistics at row {reader.line_num}")
            spacing = row["translation_stride_cells"]
            if adopted and spacing == "legacy_unrecorded":
                seen[date] = None
            elif spacing in valid_spacings:
                seen[date] = int(spacing)
            else:
                raise ValueError(f"Invalid checkpoint stride for {date}: {spacing}")
    return seen


class PopulationAttempt:
    """Retain separate attempts; a crash never produces a successful completion receipt."""

    def __init__(self, directory: Path, dates: list[str], settings_sha256: str, *, adopted: bool = False):
        parent = directory / "population-attempts"
        if parent.resolve() != parent:
            raise ValueError("Unsafe population-attempts directory")
        parent.mkdir(exist_ok=True)
        self.path = parent / f"{uuid4().hex}.json"
        self.stats = directory / "storm-stats.csv"
        self.adopted = adopted
        self.placements: dict[str, int | None] = {}
        self.dates = set(dates)
        self.record = {
            "version": 2,
            "settings_sha256": settings_sha256,
            "status": "running",
            "phase": "search",
            "search": {"requested": sorted(self.dates)},
            "items": None,
        }
        self.save()

    def save(self) -> None:
        """Publish a phase transition as a complete JSON write."""
        content = json.dumps(self.record, sort_keys=True, separators=(",", ":")) + "\n"
        atomic_write_bytes(self.path, content.encode(), immutable=False)

    def completed_placements(self) -> dict[str, int | None]:
        """Validate the statistics bytes currently on disk."""
        return _read_placements(self.stats, adopted=self.adopted)

    def _record_search(self) -> tuple[set[str], set[str]]:
        self.placements = self.completed_placements()
        seen = set(self.placements)
        missing, unexpected = self.dates - seen, seen - self.dates
        self.record["search"].update(
            completed=sorted(seen), missing=sorted(missing), unexpected=sorted(unexpected),
            fallback_dates=sorted(date for date, stride in self.placements.items() if stride == 1),
            legacy_unrecorded=sorted(date for date, stride in self.placements.items() if stride is None),
        )
        return missing, unexpected

    def check_search(self) -> None:
        """Require exact requested date coverage before ranking."""
        missing, unexpected = self._record_search()
        self.save()
        if missing or unexpected:
            raise ValueError(f"Search roster mismatch: {len(missing)} missing, {len(unexpected)} unexpected")

    def plan_items(self, rows: list[dict]) -> None:
        """Bind each rank to its requested event before writing Items."""
        self.record["phase"] = "items"
        self.record["items"] = {
            "requested": {str(row["por_rank"]): row["storm_date"].strftime("%Y-%m-%dT%H") for row in rows}
        }
        self.save()

    def check_items(self, directory: Path, duration: int) -> None:
        """Require the complete rank, event and duration roster on disk before catalog registration."""
        expected = self.record["items"]["requested"]
        completed = []
        invalid = {}
        for item_id, date in expected.items():
            path = directory / item_id / f"{item_id}.json"
            if not path.is_file():
                continue
            expected_start = datetime.strptime(date, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)
            try:
                item = json.loads(path.read_bytes())
                start = datetime.fromisoformat(item["properties"]["start_datetime"].replace("Z", "+00:00"))
                end = datetime.fromisoformat(item["properties"]["end_datetime"].replace("Z", "+00:00"))
                method = item["properties"]["stormhub:transposition_search"]
                stride = method["stride_cells"]
                observed = self.placements[date]
                if (
                    method["method"] != TRANSPOSITION_METHOD
                    or type(stride) is not int
                    or stride not in TRANSLATION_STRIDES
                    or (observed is not None and observed != stride)
                ):
                    raise ValueError("Ranked Item transposition differs from its search")
                if (
                    item.get("type") == "Feature"
                    and item.get("id") == item_id
                    and start == expected_start
                    and (end - start).total_seconds() == duration * 3600
                ):
                    completed.append(item_id)
            except (KeyError, AttributeError, TypeError, ValueError) as error:
                invalid[item_id] = type(error).__name__
        actual = {
            entry.name
            for entry in directory.iterdir()
            if entry.is_dir() and (entry.name.isdigit() or (entry / f"{entry.name}.json").is_file())
        }
        missing = sorted(set(expected) - set(completed))
        unexpected = sorted(actual - set(expected))
        self.record["items"].update(completed=completed, missing=missing, unexpected=unexpected, invalid=invalid)
        self.save()
        if missing or unexpected:
            raise ValueError(
                f"Ranked Item roster mismatch: {len(missing)} missing or incorrect, {len(unexpected)} extra"
            )

    @contextmanager
    def track(self):
        """Record caught failures; preserve search checkpoints for verified retry."""
        try:
            yield self
        except (Exception, KeyboardInterrupt) as error:
            self.record.update(status="failed", error_type=type(error).__name__)
            if isinstance(error, PopulationFailure):
                self.record["worker_failures"] = error.failed
            if self.record["phase"] == "search":
                try:
                    self._record_search()
                except (OSError, ValueError, csv.Error) as checkpoint_error:
                    self.record["checkpoint_error"] = type(checkpoint_error).__name__
            self.save()
            raise
        else:
            self.record.update(status="complete", phase="complete")
            self.save()
