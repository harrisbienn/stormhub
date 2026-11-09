"""Real local filesystem publication races and interruption recovery."""

from __future__ import annotations

import hashlib
import multiprocessing
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pystac
import pytest

from stormhub import _files, publishing
from stormhub.met.hrrr_acquisition import write_immutable
from stormhub.publishing import AuthenticatedAsset, AuthenticatedItemPublication, publish_authenticated_item


def _request(root, identity="one", status="complete"):
    asset = root / "asset.json"
    return AuthenticatedItemPublication(
        item_id=identity,
        collection_id="responses",
        collection_description="Recovery fixtures",
        geometry={"type": "Point", "coordinates": [0, 0]},
        bbox=(0, 0, 0, 0),
        start_datetime=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end_datetime=datetime(2026, 1, 2, tzinfo=timezone.utc),
        properties={"status": status},
        links=(),
        assets=(
            AuthenticatedAsset(
                key="evidence",
                href=str(asset),
                sha256=hashlib.sha256(b"{}").hexdigest(),
                size_bytes=2,
                media_type="application/json",
                roles=("metadata",),
            ),
        ),
    )


def _catalog(root):
    (root / "asset.json").write_bytes(b"{}")
    catalog = pystac.Catalog(id="recovery", description="Recovery fixtures")
    catalog.normalize_and_save(str(root / "catalog"), catalog_type=pystac.CatalogType.SELF_CONTAINED)
    return catalog


def _worker(root, identity, status, ready, results):
    catalog = pystac.Catalog.from_file(str(root / "catalog/catalog.json"))
    ready.wait(timeout=15)
    try:
        publish_authenticated_item(catalog, _request(root, identity, status), reference_path=root / "asset.json")
        results.put("ok")
    except ValueError:
        results.put("conflict")


def _immutable_worker(path, content, ready, results):
    ready.wait(timeout=15)
    try:
        write_immutable(path, content)
        results.put("ok")
    except ValueError:
        results.put("conflict")


def _parallel(target, arguments):
    context = multiprocessing.get_context("spawn")
    ready, results = context.Barrier(len(arguments) + 1), context.Queue()
    workers = [context.Process(target=target, args=(*args, ready, results)) for args in arguments]
    try:
        for worker in workers:
            worker.start()
        ready.wait(timeout=15)
        outcomes = [results.get(timeout=20) for _ in workers]
        for worker in workers:
            worker.join(timeout=15)
            assert worker.exitcode == 0
        return sorted(outcomes)
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=10)
        results.close()


def test_stale_handles_preserve_every_accepted_item(tmp_path):
    """Reload disk state instead of replacing newer index entries with old ones."""
    first = _catalog(tmp_path)
    second = pystac.Catalog.from_file(first.get_self_href())
    for handle, identity in ((first, "first"), (second, "second")):
        publish_authenticated_item(handle, _request(tmp_path, identity), reference_path=tmp_path / "asset.json")
    saved = pystac.Catalog.from_file(first.get_self_href())
    assert {item.id for item in saved.get_items(recursive=True)} == {"first", "second"}


@pytest.mark.parametrize("same_identity,conflict", [(False, False), (True, False), (True, True)])
def test_independent_process_publishers(tmp_path, same_identity, conflict):
    """Independent writers preserve both IDs, coalesce retries and reject conflicts."""
    catalog = _catalog(tmp_path)
    outcomes = _parallel(
        _worker,
        [
            (tmp_path, "one", "complete"),
            (tmp_path, "one" if same_identity else "two", "changed" if conflict else "complete"),
        ],
    )
    assert outcomes == (["conflict", "ok"] if conflict else ["ok", "ok"])
    items = list(pystac.Catalog.from_file(catalog.get_self_href()).get_items(recursive=True))
    assert {item.id for item in items} == ({"one"} if same_identity else {"one", "two"})


@pytest.mark.parametrize("stage", ["item", "collection", "catalog"])
@pytest.mark.parametrize("after", [False, True])
@pytest.mark.parametrize("existing_collection", [False, True])
def test_each_interrupted_stage_is_readable_and_retryable(tmp_path, monkeypatch, stage, after, existing_collection):
    """Reader-visible links stay complete across each commit boundary."""
    catalog = _catalog(tmp_path)
    if existing_collection:
        publish_authenticated_item(catalog, _request(tmp_path, "previous"), reference_path=tmp_path / "asset.json")
    save = publishing._save_stac_atomic

    def interrupted(value, **kwargs):
        kind = (
            "item"
            if isinstance(value, pystac.Item)
            else "collection"
            if isinstance(value, pystac.Collection)
            else "catalog"
        )
        if kind == stage and not after:
            raise OSError("injected interruption")
        save(value, **kwargs)
        if kind == stage and after:
            raise OSError("injected interruption")

    monkeypatch.setattr(publishing, "_save_stac_atomic", interrupted)
    with pytest.raises(OSError, match="injected"):
        publish_authenticated_item(catalog, _request(tmp_path), reference_path=tmp_path / "asset.json")
    visible = list(pystac.Catalog.from_file(catalog.get_self_href()).get_items(recursive=True))
    if existing_collection:
        assert "previous" in {item.id for item in visible}
    for item in visible:
        assert Path(item.get_self_href()).is_file()
        assert all(Path(asset.get_absolute_href()).is_file() for asset in item.assets.values())
    monkeypatch.setattr(publishing, "_save_stac_atomic", save)
    publish_authenticated_item(catalog, _request(tmp_path), reference_path=tmp_path / "asset.json")
    assert {item.id for item in pystac.Catalog.from_file(catalog.get_self_href()).get_items(recursive=True)} == (
        {"one", "previous"} if existing_collection else {"one"}
    )


def test_unindexed_complete_collection_is_preserved_on_different_retry(tmp_path, monkeypatch):
    """A later different publication recovers a Collection saved before the root."""
    catalog = _catalog(tmp_path)
    save = publishing._save_stac_atomic

    def interrupted(value, **kwargs):
        if type(value) is pystac.Catalog:
            raise OSError("before root")
        save(value, **kwargs)

    monkeypatch.setattr(publishing, "_save_stac_atomic", interrupted)
    with pytest.raises(OSError):
        publish_authenticated_item(catalog, _request(tmp_path), reference_path=tmp_path / "asset.json")
    monkeypatch.setattr(publishing, "_save_stac_atomic", save)
    publish_authenticated_item(catalog, _request(tmp_path, "two"), reference_path=tmp_path / "asset.json")
    assert {item.id for item in pystac.Catalog.from_file(catalog.get_self_href()).get_items(recursive=True)} == {
        "one",
        "two",
    }


@pytest.mark.parametrize("conflict", [False, True])
def test_atomic_immutable_process_race(tmp_path, conflict):
    """Only complete bytes become visible, even with simultaneous conflicting writes."""
    path = tmp_path / "receipt.json"
    outcomes = _parallel(_immutable_worker, [(path, b"first"), (path, b"other" if conflict else b"first")])
    assert outcomes == (["conflict", "ok"] if conflict else ["ok", "ok"])
    assert path.read_bytes() in ({b"first", b"other"} if conflict else {b"first"})
    assert not list(tmp_path.glob(".stormhub-*.tmp"))


def test_interrupted_partial_stage_never_creates_final_file(tmp_path, monkeypatch):
    """A failed partial payload or receipt can be safely retried."""
    path = tmp_path / "receipt.json"
    original = _files._write_content

    def partial(stream, content):
        stream.write(content[:2])
        stream.flush()
        raise OSError("partial disk write")

    monkeypatch.setattr(_files, "_write_content", partial)
    with pytest.raises(OSError, match="partial"):
        write_immutable(path, b"complete")
    assert not path.exists()
    assert not list(tmp_path.glob(".stormhub-*.tmp"))
    monkeypatch.setattr(_files, "_write_content", original)
    write_immutable(path, b"complete")
    assert path.read_bytes() == b"complete"


def test_interrupted_mutable_stage_preserves_previous_index(tmp_path, monkeypatch):
    """Partial staged index writes never replace the reader-visible previous version."""
    path = tmp_path / "catalog.json"
    path.write_bytes(b"previous complete index")

    def partial(stream, content):
        stream.write(content[:2])
        raise OSError("partial index write")

    monkeypatch.setattr(_files, "_write_content", partial)
    with pytest.raises(OSError, match="partial index"):
        _files.atomic_write_bytes(path, b"new complete index", immutable=False)
    assert path.read_bytes() == b"previous complete index"
    assert not list(tmp_path.glob(".stormhub-*.tmp"))


def test_busy_lock_is_bounded_and_never_deleted(tmp_path):
    """Lock contention fails explicitly, and later callers reuse the same lock file."""
    path = tmp_path / "publisher.lock"
    with _files.exclusive_file_lock(path):
        with pytest.raises(TimeoutError, match="publication lock"):
            with _files.exclusive_file_lock(path, timeout=0):
                pytest.fail("second lock unexpectedly acquired")
    assert path.is_file()
    with _files.exclusive_file_lock(path, timeout=0):
        assert path.is_file()


@pytest.mark.skipif(os.name != "nt", reason="Windows byte-range locks deny concurrent sentinel writes")
def test_contended_empty_lock_waits_before_initializing(tmp_path):
    """A contender must not write a new lock's sentinel while another process owns its byte."""
    import msvcrt

    path = tmp_path / "publisher.lock"
    with path.open("a+b") as owner:
        msvcrt.locking(owner.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            with pytest.raises(TimeoutError, match="publication lock"):
                with _files.exclusive_file_lock(path, timeout=0):
                    pytest.fail("contender acquired an owned empty lock")
        finally:
            owner.seek(0)
            msvcrt.locking(owner.fileno(), msvcrt.LK_UNLCK, 1)
    assert path.read_bytes() == b""
    with _files.exclusive_file_lock(path, timeout=0):
        assert path.is_file()


def test_staging_name_collision_preserves_unowned_file(tmp_path, monkeypatch):
    """A temporary-name collision cannot delete or replace another writer's stage."""
    staged = tmp_path / ".stormhub-collision.tmp"
    staged.write_bytes(b"other writer")
    monkeypatch.setattr(_files.uuid, "uuid4", lambda: SimpleNamespace(hex="collision"))
    with pytest.raises(FileExistsError):
        write_immutable(tmp_path / "receipt.json", b"requested")
    assert staged.read_bytes() == b"other writer"
    assert not (tmp_path / "receipt.json").exists()


def _crash_writer(path):
    with _files.exclusive_file_lock(path.parent / "writer.lock"):

        def interrupted(stream, content):
            stream.write(content[:2])
            stream.flush()
            os._exit(17)

        _files._write_content = interrupted
        write_immutable(path, b"complete")


def test_process_death_releases_lock_and_partial_temp_is_not_accepted(tmp_path):
    """An actual process exit releases its lock; abandoned staging never poisons retry."""
    path = tmp_path / "receipt.json"
    process = multiprocessing.get_context("spawn").Process(target=_crash_writer, args=(path,))
    process.start()
    process.join(timeout=15)
    try:
        assert process.exitcode == 17
        assert not path.exists()
        abandoned = list(tmp_path.glob(".stormhub-*.tmp"))
        assert len(abandoned) == 1 and abandoned[0].read_bytes() == b"co"
        with _files.exclusive_file_lock(tmp_path / "writer.lock", timeout=0.2):
            write_immutable(path, b"complete")
        assert path.read_bytes() == b"complete"
        assert abandoned[0].read_bytes() == b"co"
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)
