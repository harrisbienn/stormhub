"""Tests for the domain-neutral authenticated STAC publisher."""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pystac
import pytest

from stormhub.publishing import (
    AuthenticatedAsset,
    AuthenticatedItemPublication,
    PublicationLink,
    publish_authenticated_item,
)
from stormhub.utils import sha256_from_checksum


@pytest.mark.parametrize("field", ["item_id", "collection_id"])
@pytest.mark.parametrize("value", [
    "C:outside", "C:\\outside", "file:stream", "//server/share", "\\rooted", "../outside",
    "CON", "con.txt", "CON .txt", "CONIN$", "NUL", "AUX.json", "PRN", "COM1", "LPT9.txt", "COM\u00b9", "LPT\u00b2.ext",
    "name.", "name ", " name", "a\x00b", "a\nb", 'bad"name', "a*b", "a?b", "a|b", "a<b", "a>b",
])
def test_publication_identifiers_reject_unsafe_portable_names(tmp_path, field, value):
    """Reject Windows syntax on every host before attempting publication."""
    publication, _ = _publication(tmp_path)
    with pytest.raises(ValueError, match="safe path component"):
        replace(publication, **{field: value})


@pytest.mark.parametrize("value", ["response-123", "model_v2.1", "Model name", "bassin-\u00e9", "COM10"])
def test_publication_identifiers_preserve_safe_names(tmp_path, value):
    """Keep ordinary identifiers, including Unicode and internal spaces."""
    publication, _ = _publication(tmp_path)
    updated = replace(publication, item_id=value, collection_id=value)
    assert updated.item_id == value
    assert updated.collection_id == value


def _directory_link(link: Path, target: Path) -> None:
    """Use a Windows junction or POSIX symlink only inside disposable fixtures."""
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       check=True, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)


@pytest.mark.parametrize("boundary", ["collection", "item"])
def test_generated_destinations_reject_directory_link_escape(tmp_path, boundary):
    """Reject existing junctions/symlinks before writing any catalog content."""
    catalog = _catalog(tmp_path)
    publication, manifest = _publication(tmp_path)
    catalog_path = Path(catalog.get_self_href())
    before = catalog_path.read_bytes()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = catalog_path.parent / publication.collection_id
    if boundary == "item":
        link.mkdir()
        link = link / publication.item_id
    _directory_link(link, outside)
    with pytest.raises(ValueError, match="catalog directory"):
        publish_authenticated_item(catalog, publication, reference_path=manifest)
    assert list(outside.iterdir()) == []
    assert catalog_path.read_bytes() == before


def test_existing_collection_cannot_redirect_writes_outside_catalog(tmp_path):
    """A cached collection's self href is a write destination too."""
    catalog = _catalog(tmp_path)
    publication, manifest = _publication(tmp_path)
    publish_authenticated_item(catalog, publication, reference_path=manifest)
    collection = catalog.get_child(publication.collection_id)
    outside = tmp_path / "outside-collection.json"
    collection.set_self_href(str(outside))
    before = Path(catalog.get_self_href()).read_bytes()
    with pytest.raises(ValueError, match="catalog directory"):
        publish_authenticated_item(catalog, replace(publication, item_id="second"), reference_path=manifest)
    assert not outside.exists()
    assert Path(catalog.get_self_href()).read_bytes() == before


def test_explicit_item_path_remains_a_trusted_override(tmp_path):
    """An explicit caller-owned output path retains the existing API contract."""
    catalog = _catalog(tmp_path)
    publication, manifest = _publication(tmp_path)
    destination = tmp_path / "trusted-export" / "item.json"
    publish_authenticated_item(catalog, publication, reference_path=manifest, item_path=destination)
    assert pystac.Item.from_file(str(destination)).id == publication.item_id


def test_catalog_layout_cannot_replace_validated_destinations(tmp_path):
    """Inherited PySTAC strategies cannot redirect this publisher's writes."""
    class OutsideLayout(pystac.layout.BestPracticesLayoutStrategy):
        def get_href(self, stac_object, parent_dir, is_root=False):
            return str(tmp_path / "outside.json")

    catalog = _catalog(tmp_path)
    catalog.strategy = OutsideLayout()
    publication, manifest = _publication(tmp_path)
    item = publish_authenticated_item(catalog, publication, reference_path=manifest)
    assert Path(item.get_self_href()).is_relative_to(tmp_path / "catalog")
    assert not (tmp_path / "outside.json").exists()
    saved = pystac.Item.from_file(item.get_self_href())
    for asset in saved.assets.values():
        assert Path(asset.get_absolute_href()).is_file()
    assert saved.get_parent().id == publication.collection_id


def _asset(path: Path, *, key: str, metadata: dict[str, object] | None = None) -> AuthenticatedAsset:
    content = path.read_bytes()
    return AuthenticatedAsset(
        key=key,
        href=str(path),
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        media_type="application/json",
        roles=("metadata",),
        title=f"Asset {key}",
        metadata=metadata or {},
    )


def _catalog(root: Path) -> pystac.Catalog:
    catalog = pystac.Catalog(id="test-catalog", description="Test catalog")
    catalog.normalize_and_save(str(root / "catalog"), catalog_type=pystac.CatalogType.SELF_CONTAINED)
    return catalog


def _publication(tmp_path: Path) -> tuple[AuthenticatedItemPublication, Path]:
    manifest = tmp_path / "records" / "response.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"contract":"example/response/1.0"}\n', encoding="utf-8")
    source_item = tmp_path / "source" / "scenario.json"
    source_item.parent.mkdir(parents=True)
    source_item.write_text('{"type":"Feature"}\n', encoding="utf-8")
    publication = AuthenticatedItemPublication(
        item_id="response-abc123",
        collection_id="scenario-responses",
        collection_description="Caller-owned scenario responses.",
        geometry={"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
        bbox=(0.0, 0.0, 1.0, 1.0),
        start_datetime=datetime(2026, 9, 1, tzinfo=UTC),
        end_datetime=datetime(2026, 9, 2, tzinfo=UTC),
        properties={"example:status": "complete", "example:decision": "caller-owned"},
        assets=(
            _asset(manifest, key="authoritative-record"),
            _asset(source_item, key="summary", metadata={"table:row_count": 1}),
        ),
        links=(
            PublicationLink(
                rel=pystac.RelType.DERIVED_FROM,
                href=str(source_item),
                media_type=pystac.MediaType.JSON,
                title="Source scenario",
            ),
        ),
    )
    return publication, manifest


def test_publish_authenticated_item_preserves_caller_semantics_and_portable_hrefs(tmp_path: Path) -> None:
    """Preserve caller properties and write a portable, authenticated Item."""
    catalog = _catalog(tmp_path)
    publication, manifest = _publication(tmp_path)

    item = publish_authenticated_item(catalog, publication, reference_path=manifest)

    item_path = tmp_path / "catalog" / "scenario-responses" / publication.item_id / f"{publication.item_id}.json"
    saved = pystac.Item.from_file(str(item_path))
    assert saved.properties["example:status"] == "complete"
    assert saved.properties["example:decision"] == "caller-owned"
    assert not any(key.startswith("stormhub:") for key in saved.properties)
    assert set(saved.assets) == {"authoritative-record", "summary"}
    assert saved.assets["authoritative-record"].href.startswith("../../../records/")
    assert sha256_from_checksum(saved.assets["authoritative-record"].extra_fields["file:checksum"]) == (
        publication.assets[0].sha256
    )
    assert saved.assets["summary"].extra_fields["table:row_count"] == 1
    assert any(link.rel == pystac.RelType.DERIVED_FROM for link in saved.links)
    assert "https://stac-extensions.github.io/file/" in " ".join(saved.stac_extensions)
    assert "https://stac-extensions.github.io/table/" in " ".join(saved.stac_extensions)
    reloaded = pystac.Catalog.from_file(str(tmp_path / "catalog" / "catalog.json"))
    collection = reloaded.get_child("scenario-responses")
    assert collection.get_item(publication.item_id) is not None
    assert set(collection.extra_fields["item_assets"]) == {"authoritative-record", "summary"}
    assert item.id == publication.item_id


def test_publisher_fails_closed_on_changed_or_duplicate_content(tmp_path: Path) -> None:
    """Refuse tampered assets and repeated immutable Item identities."""
    catalog = _catalog(tmp_path)
    publication, manifest = _publication(tmp_path)
    changed = Path(publication.assets[1].href)
    changed.write_text("changed", encoding="utf-8")

    with pytest.raises(ValueError, match="Size mismatch|Checksum mismatch"):
        publish_authenticated_item(catalog, publication, reference_path=manifest)

    changed.write_text('{"type":"Feature"}\n', encoding="utf-8")
    publish_authenticated_item(catalog, publication, reference_path=manifest)
    with pytest.raises(ValueError, match="already published"):
        publish_authenticated_item(catalog, publication, reference_path=manifest)


def test_publication_request_rejects_ambiguous_asset_identity(tmp_path: Path) -> None:
    """Reject duplicate asset keys and caller overrides of file identity."""
    publication, _ = _publication(tmp_path)
    with pytest.raises(ValueError, match="asset keys must be unique"):
        AuthenticatedItemPublication(
            item_id=publication.item_id,
            collection_id=publication.collection_id,
            collection_description=publication.collection_description,
            geometry=publication.geometry,
            bbox=publication.bbox,
            start_datetime=publication.start_datetime,
            end_datetime=publication.end_datetime,
            properties=publication.properties,
            assets=(publication.assets[0], publication.assets[0]),
            links=publication.links,
        )

    with pytest.raises(ValueError, match="cannot override"):
        AuthenticatedAsset(
            key="invalid",
            href="missing.json",
            sha256="a" * 64,
            size_bytes=1,
            media_type="application/json",
            roles=("metadata",),
            metadata={"file:checksum": "caller-value"},
        )

    with pytest.raises(ValueError, match="safe path component"):
        AuthenticatedItemPublication(
            item_id="../outside",
            collection_id=publication.collection_id,
            collection_description=publication.collection_description,
            geometry=publication.geometry,
            bbox=publication.bbox,
            start_datetime=publication.start_datetime,
            end_datetime=publication.end_datetime,
            properties=publication.properties,
            assets=publication.assets,
            links=publication.links,
        )
