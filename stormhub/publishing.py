"""Generic publication of authenticated records as portable STAC Items."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PureWindowsPath
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit

import pystac
from pystac.extensions.file import FileExtension
from pystac.extensions.item_assets import ItemAssetsExtension
from pystac.extensions.raster import RasterExtension

from stormhub.utils import sha256_file, sha256_multihash

TABLE_EXTENSION_SCHEMA = "https://stac-extensions.github.io/table/v1.2.0/schema.json"
PROJECTION_EXTENSION_SCHEMA = "https://stac-extensions.github.io/projection/v2.0.0/schema.json"
ITEM_ASSETS_EXTENSION_SCHEMA = ItemAssetsExtension.get_schema_uri()
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _required_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _path_component(value: str, name: str) -> str:
    normalized = _required_text(value, name)
    # Apply Windows rules on every host so a published tree stays portable.
    # Reject normalization aliases rather than silently changing Item identities.
    if (
        normalized != value
        or normalized in {".", ".."}
        or normalized.endswith(".")
        or any(character in '<>:"/\\|?*' or ord(character) < 32 for character in normalized)
        or PureWindowsPath(normalized).is_reserved()
    ):
        raise ValueError(f"{name} must be one safe path component")
    return normalized


def _confined_path(path: Path, catalog_dir: Path) -> Path:
    """Resolve existing symlinks/junctions and constrain publication writes."""
    resolved = path.resolve()
    if not resolved.is_relative_to(catalog_dir):
        raise ValueError("Publication destination must remain inside the catalog directory")
    return resolved


def _is_remote_href(href: str) -> bool:
    """Return whether an href has a non-Windows URI scheme."""
    if re.match(r"^[A-Za-z]:[/\\]", href):
        return False
    return bool(urlsplit(href).scheme)


def _resolve_href(href: str, reference_path: Path) -> str:
    """Resolve a reference-relative href while preserving remote IRIs."""
    if _is_remote_href(href):
        return href
    candidate = Path(href)
    if candidate.is_absolute():
        return str(candidate.resolve())
    return str((reference_path.parent / candidate).resolve())


def _publication_href(href: str, reference_path: Path, item_path: Path) -> str:
    """Rebase a local reference so its serialized Item href is portable."""
    resolved = _resolve_href(href, reference_path)
    if _is_remote_href(resolved):
        return resolved
    try:
        relative = os.path.relpath(resolved, start=item_path.parent.resolve())
    except ValueError as exc:
        raise ValueError(f"Cannot publish href relative to STAC Item: {href}") from exc
    return Path(relative).as_posix()


@dataclass(frozen=True)
class AuthenticatedAsset:
    """One caller-owned asset whose identity StormHub verifies but does not interpret."""

    key: str
    href: str
    sha256: str
    size_bytes: int
    media_type: str
    roles: tuple[str, ...]
    title: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate and freeze the caller-owned asset description."""
        object.__setattr__(self, "key", _required_text(self.key, "asset key"))
        object.__setattr__(self, "href", _required_text(self.href, f"asset {self.key!r} href"))
        if not _SHA256.fullmatch(self.sha256):
            raise ValueError(f"asset {self.key!r} sha256 must contain 64 lowercase hexadecimal characters")
        if not isinstance(self.size_bytes, int) or isinstance(self.size_bytes, bool) or self.size_bytes < 0:
            raise ValueError(f"asset {self.key!r} size_bytes must be a nonnegative integer")
        object.__setattr__(self, "media_type", _required_text(self.media_type, f"asset {self.key!r} media_type"))
        if not self.roles or any(not isinstance(role, str) or not role.strip() for role in self.roles):
            raise ValueError(f"asset {self.key!r} roles must contain non-empty strings")
        if len(self.roles) != len(set(self.roles)):
            raise ValueError(f"asset {self.key!r} roles must be unique")
        if self.title is not None:
            object.__setattr__(self, "title", _required_text(self.title, f"asset {self.key!r} title"))
        forbidden = {"file:size", "file:checksum"}.intersection(self.metadata)
        if forbidden:
            raise ValueError(f"asset {self.key!r} metadata cannot override {sorted(forbidden)}")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class PublicationLink:
    """One caller-owned STAC relationship projected without domain interpretation."""

    rel: str
    href: str
    media_type: str | None = None
    title: str | None = None

    def __post_init__(self) -> None:
        """Validate one caller-owned link description."""
        object.__setattr__(self, "rel", _required_text(self.rel, "link rel"))
        object.__setattr__(self, "href", _required_text(self.href, f"link {self.rel!r} href"))
        if self.media_type is not None:
            object.__setattr__(self, "media_type", _required_text(self.media_type, f"link {self.rel!r} media_type"))
        if self.title is not None:
            object.__setattr__(self, "title", _required_text(self.title, f"link {self.rel!r} title"))


@dataclass(frozen=True)
class AuthenticatedItemPublication:
    """Domain-neutral request for one immutable Item in a local STAC Catalog."""

    item_id: str
    collection_id: str
    collection_description: str
    geometry: Mapping[str, Any]
    bbox: tuple[float, ...]
    start_datetime: datetime
    end_datetime: datetime
    properties: Mapping[str, Any]
    assets: tuple[AuthenticatedAsset, ...]
    links: tuple[PublicationLink, ...]
    license: str = "proprietary"

    def __post_init__(self) -> None:
        """Validate and freeze the domain-neutral publication request."""
        object.__setattr__(self, "item_id", _path_component(self.item_id, "item_id"))
        object.__setattr__(self, "collection_id", _path_component(self.collection_id, "collection_id"))
        object.__setattr__(
            self,
            "collection_description",
            _required_text(self.collection_description, "collection_description"),
        )
        object.__setattr__(self, "license", _required_text(self.license, "license"))
        if not isinstance(self.geometry, Mapping) or not self.geometry:
            raise ValueError("geometry must be a non-empty GeoJSON object")
        if len(self.bbox) not in {4, 6} or any(
            isinstance(value, bool) or not isinstance(value, (int, float)) for value in self.bbox
        ):
            raise ValueError("bbox must contain four or six numeric coordinates")
        for name, value in (("start_datetime", self.start_datetime), ("end_datetime", self.end_datetime)):
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.end_datetime < self.start_datetime:
            raise ValueError("end_datetime must not precede start_datetime")
        if not isinstance(self.properties, Mapping):
            raise ValueError("properties must be a mapping")
        if not self.assets:
            raise ValueError("assets must contain at least one authenticated asset")
        asset_keys = [asset.key for asset in self.assets]
        if len(asset_keys) != len(set(asset_keys)):
            raise ValueError("asset keys must be unique")
        object.__setattr__(self, "geometry", MappingProxyType(dict(self.geometry)))
        object.__setattr__(self, "properties", MappingProxyType(dict(self.properties)))


def _verify_asset(asset: AuthenticatedAsset, reference_path: Path) -> None:
    resolved = _resolve_href(asset.href, reference_path)
    if _is_remote_href(resolved):
        return
    path = Path(resolved)
    if not path.is_file():
        raise ValueError(f"Authenticated asset {asset.key!r} does not exist: {path}")
    if path.stat().st_size != asset.size_bytes:
        raise ValueError(f"Size mismatch for authenticated asset {asset.key!r}: {path}")
    if sha256_file(path) != asset.sha256:
        raise ValueError(f"Checksum mismatch for authenticated asset {asset.key!r}: {path}")


def _stac_asset(asset: AuthenticatedAsset, reference_path: Path, item_path: Path) -> pystac.Asset:
    extra_fields = dict(asset.metadata)
    extra_fields.update(
        {
            "file:size": asset.size_bytes,
            "file:checksum": sha256_multihash(asset.sha256),
        }
    )
    return pystac.Asset(
        href=_publication_href(asset.href, reference_path, item_path),
        title=asset.title,
        media_type=asset.media_type,
        roles=list(asset.roles),
        extra_fields=extra_fields,
    )


def _declare_extensions(item: pystac.Item, assets: tuple[AuthenticatedAsset, ...]) -> None:
    metadata_keys = {key for asset in assets for key in asset.metadata}
    if any(key.startswith("proj:") for key in metadata_keys):
        item.stac_extensions.append(PROJECTION_EXTENSION_SCHEMA)
    if any(key.startswith("raster:") for key in metadata_keys):
        RasterExtension.add_to(item)
    if any(key.startswith("table:") for key in metadata_keys):
        item.stac_extensions.append(TABLE_EXTENSION_SCHEMA)


def authenticated_item_to_stac(
    publication: AuthenticatedItemPublication,
    *,
    reference_path: str | Path,
    item_path: str | Path,
    verify_files: bool = True,
) -> pystac.Item:
    """Project a caller-owned publication request into a portable STAC Item."""
    reference = Path(reference_path).resolve()
    output = Path(item_path).resolve()
    if not reference.is_file():
        raise ValueError(f"Publication reference file does not exist: {reference}")
    if verify_files:
        for asset in publication.assets:
            _verify_asset(asset, reference)
    item = pystac.Item(
        id=publication.item_id,
        geometry=dict(publication.geometry),
        bbox=list(publication.bbox),
        datetime=None,
        properties=dict(publication.properties),
        start_datetime=publication.start_datetime,
        end_datetime=publication.end_datetime,
        collection=publication.collection_id,
    )
    item.set_self_href(str(output))
    FileExtension.add_to(item)
    _declare_extensions(item, publication.assets)
    for link in publication.links:
        item.add_link(
            pystac.Link(
                rel=link.rel,
                target=_publication_href(link.href, reference, output),
                media_type=link.media_type,
                title=link.title,
            )
        )
    for asset in publication.assets:
        item.add_asset(asset.key, _stac_asset(asset, reference, output))
    return item


def _new_collection(publication: AuthenticatedItemPublication, item: pystac.Item, path: Path) -> pystac.Collection:
    extent = pystac.Extent(
        spatial=pystac.SpatialExtent([item.bbox]),
        temporal=pystac.TemporalExtent([[item.common_metadata.start_datetime, item.common_metadata.end_datetime]]),
    )
    collection = pystac.Collection(
        id=publication.collection_id,
        description=publication.collection_description,
        extent=extent,
        license=publication.license,
    )
    collection.set_self_href(str(path))
    return collection


def _item_asset_definition(asset: pystac.Asset) -> dict[str, object]:
    definition: dict[str, object] = {}
    if asset.title:
        definition["title"] = asset.title
    if asset.media_type:
        definition["type"] = asset.media_type
    if asset.roles:
        definition["roles"] = list(asset.roles)
    return definition


def _update_item_assets(collection: pystac.Collection, item: pystac.Item) -> None:
    definitions = dict(collection.extra_fields.get("item_assets", {}))
    for key, asset in item.assets.items():
        definitions.setdefault(key, _item_asset_definition(asset))
    collection.extra_fields["item_assets"] = definitions
    if ITEM_ASSETS_EXTENSION_SCHEMA not in collection.stac_extensions:
        collection.stac_extensions.append(ITEM_ASSETS_EXTENSION_SCHEMA)


def publish_authenticated_item(
    catalog: pystac.Catalog,
    publication: AuthenticatedItemPublication,
    *,
    reference_path: str | Path,
    item_path: str | Path | None = None,
    verify_files: bool = True,
) -> pystac.Item:
    """Append one authenticated, caller-owned Item to a saved local STAC Catalog.

    Generated Item and Collection paths stay within the resolved catalog directory,
    including through existing symlinks/junctions. An explicit ``item_path`` is a
    trusted-caller override and may be outside that directory. Do not pass a reader's
    input through that override. The catalog tree must not be writable by untrusted
    processes; path checks do not prevent concurrent filesystem substitution.
    """
    catalog_href = catalog.get_self_href()
    if catalog_href is None or _is_remote_href(catalog_href):
        raise ValueError("Publishing requires a saved local STAC Catalog")
    catalog_dir = Path(catalog_href).resolve().parent
    collection_dir = _confined_path(catalog_dir / publication.collection_id, catalog_dir)
    collection_path = _confined_path(collection_dir / "collection.json", catalog_dir)
    output = (
        Path(item_path).resolve()
        if item_path is not None
        else _confined_path(collection_dir / publication.item_id / f"{publication.item_id}.json", catalog_dir)
    )
    collection = catalog.get_child(publication.collection_id)
    if collection is not None and not isinstance(collection, pystac.Collection):
        raise ValueError(f"Catalog child {publication.collection_id!r} is not a STAC Collection")
    if collection is not None:
        collection_href = collection.get_self_href()
        if collection_href is None or _is_remote_href(collection_href):
            raise ValueError("Collection must be saved locally inside the catalog directory")
        _confined_path(Path(collection_href), catalog_dir)
    existing = collection.get_item(publication.item_id, recursive=False) if collection is not None else None
    if existing is not None or output.exists():
        raise ValueError(f"Authenticated Item {publication.item_id!r} is already published")
    item = authenticated_item_to_stac(
        publication,
        reference_path=reference_path,
        item_path=output,
        verify_files=verify_files,
    )
    if collection is None:
        collection = _new_collection(publication, item, collection_path)
        collection.set_parent(catalog)
        catalog.add_child(collection, set_parent=False)
    _update_item_assets(collection, item)
    # PySTAC's inherited layout strategy can otherwise replace the validated hrefs.
    item.set_parent(collection)
    collection.add_item(item, set_parent=False)
    item.save_object(include_self_link=False)
    collection.update_extent_from_items()
    collection.save_object(include_self_link=False)
    catalog.save_object(include_self_link=False)
    return item
