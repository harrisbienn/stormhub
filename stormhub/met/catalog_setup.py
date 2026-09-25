"""Prepare checksum-pinned domains and safely create notebook-driven catalogs."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from functools import reduce
from hashlib import sha256
import json
import logging
from pathlib import Path, PureWindowsPath
import re
from typing import Any, Literal, TYPE_CHECKING

import geopandas as gpd
from shapely.geometry import Polygon, mapping, shape

from stormhub.utils import sha256_file, validate_config

if TYPE_CHECKING:
    from matplotlib.axes import Axes
    from stormhub.met.storm_catalog import StormCatalog

# Settings retain the existing extensible JSON config, including package-specific params.
CatalogSettings = dict[str, Any]
ExistingCatalog = Literal["error", "reuse"]
DOMAIN_KEYS = ("watershed", "transposition_region")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreparedDomain:
    """A complete WGS84 polygon and the identity of its unmodified source."""

    geometry: Polygon
    source_path: Path
    source_sha256: str
    source_features: int
    source_crs: str

    def summary(self) -> dict[str, object]:
        """Return inspection metadata without printing or requiring IPython."""
        return {
            "path": str(self.source_path),
            "source_features": self.source_features,
            "source_crs": self.source_crs,
            "prepared_crs": "EPSG:4326",
            "bounds": self.geometry.bounds,
            "source_sha256": self.source_sha256,
        }


def _component(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value)
        or value.endswith(".")
        or PureWindowsPath(value).is_reserved()
    ):
        raise ValueError(f"{label} must be a portable filename component: {value!r}")
    return value


def _validate_settings(settings: CatalogSettings) -> None:
    if not isinstance(settings, dict):
        raise ValueError("Catalog settings must be a JSON object")
    for key in DOMAIN_KEYS:
        if not isinstance(settings.get(key), dict):
            raise ValueError(f"{key} must be a domain configuration object")
        for field in ("id", "geometry_file", "description"):
            if not isinstance(settings[key].get(field), str) or not settings[key][field].strip():
                raise ValueError(f"{key}.{field} must be nonempty text")
    validate_config(settings)
    _component(settings.get("catalog_id"), "catalog_id")
    for key in DOMAIN_KEYS:
        _component(settings[key]["id"], f"{key}.id")
        digest = settings[key].get("source_sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"{key}.source_sha256 must be a lowercase SHA-256 digest")
    if settings["watershed"]["id"] == settings["transposition_region"]["id"]:
        raise ValueError("Watershed and transposition region IDs must differ")


def load_catalog_settings(path: str | Path) -> CatalogSettings:
    """Read the shared JSON settings and validate domain identities and checksums."""
    settings = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(settings, dict):
        raise ValueError(f"Catalog settings must be a JSON object: {path}")
    _validate_settings(settings)
    return settings


def prepare_domain(path: str | Path, expected_sha256: str) -> PreparedDomain:
    """Verify source bytes, reproject and union all features into one valid polygon.

    Sources must declare a CRS and contain valid polygonal features. No repair,
    simplification, buffering, or selection of a largest component is performed.
    The source is checked again after reading to detect changes during ingestion.
    """
    path = Path(path).resolve()
    if sha256_file(path) != expected_sha256:
        raise ValueError(f"Source checksum changed; review the input identity: {path}")
    source = gpd.read_file(path)
    if source.empty or source.crs is None:
        raise ValueError(f"Expected polygon features with a declared CRS: {path}")
    if any(
        geom is None or geom.is_empty or not geom.is_valid or geom.geom_type not in ("Polygon", "MultiPolygon")
        for geom in source.geometry
    ):
        raise ValueError(f"Source contains missing, empty, invalid, or non-polygon geometry: {path}")
    projected = source.to_crs("EPSG:4326")
    # Pairwise union also supports the installed Shapely/NumPy bulk-union incompatibility.
    polygon = reduce(lambda left, right: left.union(right), projected.geometry)
    if polygon.geom_type == "MultiPolygon" and len(polygon.geoms) == 1:
        polygon = polygon.geoms[0]
    if not isinstance(polygon, Polygon) or polygon.is_empty or not polygon.is_valid:
        raise ValueError(f"Combined domain must be one valid Polygon; got {polygon.geom_type}: {path}")
    if sha256_file(path) != expected_sha256:
        raise ValueError(f"Source changed during geometry preparation: {path}")
    return PreparedDomain(polygon, path, expected_sha256, len(source), str(source.crs))


def prepare_catalog_domains(settings: CatalogSettings, repository_root: str | Path) -> dict[str, PreparedDomain]:
    """Resolve portable input paths and prepare both configured domains without writes."""
    _validate_settings(settings)
    root = Path(repository_root).resolve()
    domains = {}
    for key in DOMAIN_KEYS:
        path = Path(settings[key]["geometry_file"])
        if path.is_absolute() or PureWindowsPath(str(path)).drive or not (root / path).resolve().is_relative_to(root):
            raise ValueError(f"{key}.geometry_file must resolve within the repository: {path}")
        domains[key] = prepare_domain(root / path, settings[key]["source_sha256"])
    return domains


def plot_catalog_domains(domains: dict[str, PreparedDomain], *, title: str = "Catalog domains") -> Axes:
    """Plot the prepared footprints and return axes for notebook customization."""
    region = gpd.GeoSeries([domains["transposition_region"].geometry], crs="EPSG:4326")
    watershed = gpd.GeoSeries([domains["watershed"].geometry], crs="EPSG:4326")
    ax = region.boundary.plot(figsize=(8, 8), color="tab:orange", linewidth=2)
    watershed.boundary.plot(ax=ax, color="tab:blue", linewidth=1)
    ax.set(title=title, xlabel="Longitude", ylabel="Latitude")
    return ax


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2) + "\n").encode("utf-8")


def _check_mode(existing: ExistingCatalog) -> None:
    if existing not in ("error", "reuse"):
        raise ValueError("existing must be 'error' or 'reuse'; replacement requires a new catalog ID")


def _verify_inputs(directory: Path, payloads: dict[str, bytes]) -> None:
    for name, expected in payloads.items():
        path = directory / name
        if not path.resolve().is_relative_to(directory) or not path.is_file():
            raise ValueError(f"Missing or external prepared input; inspect the incomplete catalog: {path}")
        actual = path.read_bytes()
        matches = actual == expected if name.endswith(".geojson") else json.loads(actual) == json.loads(expected)
        if not matches:
            raise ValueError(f"Prepared input differs; use a new catalog ID instead of overwriting: {path}")


def prepare_catalog_inputs(
    settings: CatalogSettings,
    repository_root: str | Path,
    catalog_root: str | Path,
    *,
    existing: ExistingCatalog = "error",
    config_filename: str = "catalog-config.json",
) -> Path:
    """Write fresh inputs or verify/reuse identical inputs without modifying them.

    Geometry preparation completes before creating the destination. Reuse checks
    saved settings, runtime config and exact prepared geometry bytes. It never
    fills missing files or overwrites conflicting inputs. Interrupted preparation
    requires inspection; an interrupted AORC base-catalog build can be retried
    after these complete inputs pass verification.
    """
    _check_mode(existing)
    _validate_settings(settings)
    _component(config_filename, "config_filename")
    if config_filename in ("catalog.json", "params-config.json", "inputs", "hydro_domains"):
        raise ValueError("config_filename conflicts with a catalog-managed path")
    root = Path(catalog_root).resolve()
    directory = root / settings["catalog_id"]
    if directory.resolve() != directory:
        raise ValueError(f"Catalog directory must not be a symlink or junction: {directory}")
    if directory.exists() and existing == "error":
        raise FileExistsError(f"Catalog directory exists: {directory}; select existing='reuse' or a new catalog ID")
    domains = prepare_catalog_domains(settings, repository_root)
    runtime = deepcopy(settings)
    payloads = {"params-config.json": _json_bytes(settings)}
    for key, domain in domains.items():
        name = f"inputs/{settings[key]['id']}.geojson"
        feature = {"type": "Feature", "properties": {}, "geometry": mapping(domain.geometry)}
        payload = (
            json.dumps({"type": "FeatureCollection", "features": [feature]}, separators=(",", ":")) + "\n"
        ).encode()
        payloads[name] = payload
        runtime[key].update(
            source_geometry_file=settings[key]["geometry_file"],
            geometry_file=str(directory / name),
            prepared_sha256=sha256(payload).hexdigest(),
        )
    payloads[config_filename] = _json_bytes(runtime)
    if directory.exists():
        _verify_inputs(directory, payloads)
        logger.info("Reusing verified catalog inputs: %s", directory)
    else:
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "inputs").mkdir()
        for name, payload in payloads.items():
            with (directory / name).open("xb") as stream:
                stream.write(payload)
        logger.info("Prepared catalog inputs: %s", directory)
    return directory / config_filename


def create_prepared_catalog(
    config_path: str | Path,
    *,
    existing: ExistingCatalog = "error",
    description: str = "",
) -> StormCatalog:
    """Create the AORC-dependent base catalog or load a verified existing catalog.

    Reuse is read-only for completed catalogs. Without catalog.json, retry is
    allowed only for a preparation workspace containing inputs and the expected
    base-domain JSON files, never event collections or other products. The older
    low-level new_catalog API is deliberately unchanged.
    """
    from stormhub.met.storm_catalog import StormCatalog, new_catalog

    _check_mode(existing)
    config_path = Path(config_path).absolute()
    directory = config_path.parent
    if directory.resolve() != directory or config_path.resolve() != config_path:
        raise ValueError("Prepared catalog paths must not traverse symlinks or junctions")
    config = load_catalog_settings(config_path)
    settings = load_catalog_settings(directory / "params-config.json")
    if directory.name != settings["catalog_id"]:
        raise ValueError("Prepared catalog directory does not match catalog_id")
    expected_config = deepcopy(settings)
    for key in DOMAIN_KEYS:
        path = directory / "inputs" / f"{settings[key]['id']}.geojson"
        if not path.resolve().is_relative_to(directory):
            raise ValueError(f"Prepared domain resolves outside the catalog: {path}")
        digest = sha256_file(path)
        expected_config[key].update(
            source_geometry_file=settings[key]["geometry_file"], geometry_file=str(path), prepared_sha256=digest
        )
    if config != expected_config:
        raise ValueError("Prepared config or geometry checksums differ from saved settings; refusing creation/reuse")
    catalog_path = directory / "catalog.json"
    if catalog_path.exists():
        if existing == "error":
            raise FileExistsError(f"Catalog exists: {catalog_path}; use existing='reuse' to load it without writes")
        if catalog_path.resolve() != catalog_path:
            raise ValueError("Catalog must not be a symlink or junction")
        saved_catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        expected_domains = {
            "Watershed": settings["watershed"]["id"],
            "Transposition Region": settings["transposition_region"]["id"],
            "Valid Transposition Region": settings["transposition_region"]["id"] + "_valid",
        }
        for title, domain_id in expected_domains.items():
            links = [link for link in saved_catalog.get("links", []) if link.get("title") == title]
            expected_path = directory / "hydro_domains" / f"{domain_id}.json"
            if (
                len(links) != 1
                or (directory / links[0].get("href", "")).resolve() != expected_path
                or expected_path.resolve() != expected_path
                or not expected_path.is_file()
            ):
                raise ValueError(f"Existing catalog must link to its local prepared {title} Item")
        catalog = StormCatalog.from_file(str(catalog_path))
        if catalog.id != settings["catalog_id"]:
            raise ValueError("Existing catalog ID differs from the prepared settings")
        for key in DOMAIN_KEYS:
            item = getattr(catalog, key)
            prepared = json.loads(Path(config[key]["geometry_file"]).read_text(encoding="utf-8"))
            if item.id != settings[key]["id"] or not shape(item.geometry).equals(
                shape(prepared["features"][0]["geometry"])
            ):
                raise ValueError(f"Existing catalog {key} differs from its prepared domain")
        valid_region = catalog.valid_transposition_region
        valid_geometry = shape(valid_region.geometry)
        if (
            valid_region.id != expected_domains["Valid Transposition Region"]
            or valid_geometry.is_empty
            or not valid_geometry.is_valid
        ):
            raise ValueError("Existing catalog has an invalid transposition-region Item")
        logger.info("Loading existing catalog without changes: %s", catalog_path)
        return catalog
    allowed = {"inputs", "params-config.json", config_path.name, "hydro_domains"}
    if any(path.name not in allowed for path in directory.iterdir()):
        raise ValueError("Incomplete catalog contains unexpected products; inspect it instead of recreating it")
    domain_dir = directory / "hydro_domains"
    if domain_dir.exists():
        expected_names = {f"{settings[key]['id']}.json" for key in DOMAIN_KEYS}
        expected_names.add(f"{settings['transposition_region']['id']}_valid.json")
        if domain_dir.resolve() != domain_dir or any(
            path.name not in expected_names or not path.is_file() or path.resolve() != path
            for path in domain_dir.iterdir()
        ):
            raise ValueError("Unexpected base-domain files; inspect the incomplete catalog before retrying")
        if existing == "error":
            raise FileExistsError("Partial base catalog exists; select existing='reuse' to retry with verified inputs")
    return new_catalog(settings["catalog_id"], str(config_path), str(directory.parent), description)
