"""Utility functions for stormhub."""

import hashlib
import json
import logging
import socket
from datetime import datetime, timedelta
from typing import List
import os

from pystac import Link, Collection
from shapely.geometry import mapping, shape

STORMHUB_REF_LINK = Link(
    rel="Processing",
    target="https://github.com/Dewberry/stormhub",
    title="Source Code",
    media_type="text/html",
    extra_fields={"Description": "Source code used to generate STAC objects"},
)

SHA256_MULTIHASH_PREFIX = "1220"


def sha256_file(path: str | os.PathLike, chunk_size: int = 1024 * 1024) -> str:
    """Return the lowercase SHA-256 digest of a file without loading it all into memory."""
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_multihash(sha256: str) -> str:
    """Encode a hexadecimal SHA-256 digest using the STAC File extension multihash form."""
    if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
        raise ValueError("sha256 must be a 64-character lowercase hexadecimal digest")
    return f"{SHA256_MULTIHASH_PREFIX}{sha256}"


def sha256_from_checksum(checksum: str | None) -> str | None:
    """Extract a SHA-256 digest from common STAC checksum representations."""
    if checksum is None:
        return None
    normalized = checksum.lower()
    if normalized.startswith(SHA256_MULTIHASH_PREFIX) and len(normalized) == 68:
        normalized = normalized[len(SHA256_MULTIHASH_PREFIX) :]
    elif normalized.startswith("sha256:"):
        normalized = normalized.removeprefix("sha256:")
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError(f"Unsupported checksum representation: {checksum}")
    return normalized


def is_port_in_use(port: int = 8080, host: str = "http://localhost") -> bool:
    """Check if a given port is already in use."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return False
        except socket.error:
            return True


def load_config(config_file: str) -> dict:
    """Load a json config file."""
    with open(config_file, "r", encoding="utf-8") as f:
        return json.load(f)


def validate_config(config: dict) -> dict:
    """Validate a config dictionary against required keys."""
    required_keys = {
        "watershed": ["id", "geometry_file", "description"],
        "transposition_region": ["id", "geometry_file", "description"],
    }

    for key, sub_keys in required_keys.items():
        if key not in config:
            raise ValueError(f"Missing required section: {key}")
        for sub_key in sub_keys:
            if sub_key not in config[key] or not config[key][sub_key]:
                raise ValueError(f"Missing value for {sub_key} in section {key}")
    return config


def generate_date_range(
    start_date: str, end_date: str, every_n_hours: int = 6, date_format: str = "%Y-%m-%d", months: List[int] = None
) -> List[datetime]:
    """Generate a list of datetime objects at a given interval between start and end dates."""
    if months is None:
        months = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]

    start = datetime.strptime(start_date, date_format)
    end = datetime.strptime(end_date, date_format)

    date_range = []
    current_date = start
    while current_date <= end:
        if current_date.month in months:
            date_range.append(current_date)
            current_date += timedelta(hours=every_n_hours)

    return date_range


def create_feature_collection_from_items(
    collection: Collection, output_geojson: str, select_properties: str = "aorc:statistics"
):
    """Generate a geojson feature collection from a collection of STAC items."""
    features = []
    for item in collection.get_all_items():
        geom = shape(item.geometry)
        if geom.is_empty:
            continue

        feature = {
            "type": "Feature",
            "geometry": mapping(geom),
            "properties": {
                "id": item.id,
                select_properties: item.properties.get(select_properties),
                # **item.properties,
            },
        }
        features.append(feature)

    feature_collection = {"type": "FeatureCollection", "features": features}

    with open(output_geojson, "w", encoding="utf-8") as f:
        json.dump(feature_collection, f, indent=4)

    logging.info("FeatureCollection saved to %s", output_geojson)


class StacPathManager:
    """Build consistent paths for STAC items and collections assuming a top-level local catalog."""

    def __init__(self, local_catalog_dir: str):
        self._catalog_dir = os.path.abspath(local_catalog_dir)

    @property
    def catalog_dir(self):
        """Build Catalog directory path."""
        return self._catalog_dir

    @property
    def catalog_file(self):
        """Build Catalog file path."""
        return os.path.join(self._catalog_dir, "catalog.json")

    def storm_collection_id(self, duration: int) -> str:
        """Build storm collection id."""
        return f"{duration}hr-events"

    def catalog_item(self, item_id: str) -> str:
        """Build Catalog item path."""
        return os.path.join(self.catalog_dir, item_id, f"{item_id}.json")

    def catalog_asset(self, item_id: str, asset_dir: str = "hydro_domains") -> str:
        """Build Catalog asset path."""
        return os.path.join(self.catalog_dir, asset_dir, f"{item_id}.json")

    def collection_file(self, collection_id: str) -> str:
        """Build Collection file path."""
        return os.path.join(self.catalog_dir, collection_id, "collection.json")

    def collection_dir(self, collection_id: str) -> str:
        """Build Collection directory path."""
        return os.path.join(self.catalog_dir, collection_id)

    def collection_asset(self, collection_id: str, filename: str) -> str:
        """Build Collection asset path."""
        return os.path.join(self.catalog_dir, collection_id, filename)

    def collection_item_dir(self, collection_id: str, item_id: str) -> str:
        """Build Collection item directory path."""
        return os.path.join(self.catalog_dir, collection_id, item_id)

    def collection_item(self, collection_id: str, item_id: str) -> str:
        """Build Collection item path."""
        return os.path.join(self.catalog_dir, collection_id, item_id, f"{item_id}.json")

    def collection_item_asset(self, collection_id: str, item_id: str, filename: str) -> str:
        """Build Collection item asset path."""
        return os.path.join(self.catalog_dir, collection_id, item_id, filename)


def file_table(data: dict, col1: str, col2: str):
    """Convert a dictionary into a list of dictionaries, suitable for creating a table."""
    table = []
    for k, v in data.items():
        table.append({col1: k, col2: v})
    return table
