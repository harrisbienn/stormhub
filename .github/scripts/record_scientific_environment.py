"""Record scientific CI installation provenance, not a deployment lock or acceptance."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib
import importlib.metadata as metadata
import importlib.util
import json
from pathlib import Path
import platform
import re
import site
import ssl
import subprocess
import sys
from urllib.parse import unquote, urlsplit, urlunsplit
from urllib.request import url2pathname

BOOTSTRAP = {"pip", "setuptools", "wheel"}
NATIVE_VERSIONS = {
    "fiona": (("gdal", "__gdal_version__"),),
    "rasterio": (("gdal", "__gdal_version__"), ("proj", "__proj_version__"), ("geos", "__geos_version__")),
    "pyogrio": (("gdal", "__gdal_version__"), ("geos", "__gdal_geos_version__")),
    "pyproj": (("proj", "proj_version_str"),),
    "shapely": (("geos", "geos_version_string"),),
}


def normalized_name(value: str) -> str:
    """Normalize a valid distribution name without accepting ambiguous characters."""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        raise ValueError("Invalid distribution name")
    return re.sub(r"[-_.]+", "-", value).lower()


def installed_distributions(distributions) -> dict:
    """Reject duplicate metadata records even when their spelling differs."""
    result = {}
    for distribution in distributions:
        name = normalized_name(distribution.metadata["Name"])
        if name in result:
            raise ValueError("Duplicate normalized installed distribution: " + name)
        result[name] = distribution
    return result


def artifact_identity(download: dict, name: str, repository: Path) -> dict:
    """Keep public wheel identity while excluding credentials, query strings and host paths."""
    parts = urlsplit(download.get("url", ""))
    if parts.username is not None or parts.password is not None:
        raise ValueError("Credential-bearing artifact URLs are prohibited")
    if parts.scheme == "file":
        source = Path(url2pathname(parts.path)).resolve()
        directory = download.get("dir_info")
        if (name != "stormhub" or parts.netloc not in ("", "localhost") or source != repository.resolve()
                or not isinstance(directory, dict) or directory.get("editable")):
            raise ValueError("Only the non-editable local StormHub repository is an allowed local source")
        return {"kind": "local-project", "repository": ".", "wheel_sha256": None,
                "limit": "pip reports the local source directory, not the transient built wheel identity"}
    try:
        port = parts.port
    except ValueError as error:
        raise ValueError("Invalid artifact URL port") from error
    if parts.scheme != "https" or parts.hostname != "files.pythonhosted.org" or port not in (None, 443):
        raise ValueError("Remote artifacts must be HTTPS PyPI files.pythonhosted.org wheels")
    filename = unquote(parts.path.rsplit("/", 1)[-1])
    if not re.fullmatch(r"[A-Za-z0-9_.+!-]+\.whl", filename):
        raise ValueError("Remote artifact is not a supported wheel filename")
    archive = download.get("archive_info", {})
    checksum = archive.get("hashes", {}).get("sha256")
    legacy = archive.get("hash")
    if checksum is None and isinstance(legacy, str) and legacy.startswith("sha256="):
        checksum = legacy[7:]
    if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise ValueError("Reported wheel requires an exact SHA256 digest")
    if legacy and legacy != "sha256=" + checksum:
        raise ValueError("Conflicting pip artifact digests")
    return {"kind": "wheel", "url": urlunsplit(("https", "files.pythonhosted.org", parts.path, "", "")),
            "filename": filename, "sha256": checksum,
            "digest_authority": "pip install report; downloaded wheel is not rehashed by this helper"}


def reconcile_report(report: dict, installed: dict, repository: Path) -> dict:
    """Require each reported installation to match one current installed distribution."""
    if report.get("version") != "1" or not isinstance(report.get("install"), list):
        raise ValueError("Unsupported pip install report")
    result = {}
    for entry in report["install"]:
        item = entry["metadata"]
        name = normalized_name(item["name"])
        if name in result:
            raise ValueError("Duplicate normalized pip-report distribution: " + name)
        if name not in installed or item["version"] != installed[name].version:
            raise ValueError("Pip report version differs from installed inventory: " + name)
        result[name] = {"version": item["version"], "requested": bool(entry.get("requested")),
                        "artifact": artifact_identity(entry["download_info"], name, repository)}
    if "stormhub" not in result or "pytest" not in result:
        raise ValueError("Scientific report must include the StormHub and pytest installation")
    return result


def verify_import(name: str, distribution, prefix: Path):
    """Require a non-editable import owned by this interpreter's installed distribution."""
    direct = json.loads(distribution.read_text("direct_url.json") or "{}")
    if direct.get("dir_info", {}).get("editable"):
        raise ValueError("Editable scientific import is prohibited: " + name)
    if not Path(distribution.locate_file("")).resolve().is_relative_to(prefix):
        raise ValueError("Distribution metadata is outside the interpreter prefix: " + name)
    spec = importlib.util.find_spec(name)
    if spec is None or spec.origin is None:
        raise ValueError("Required installed import is missing: " + name)
    origin = Path(spec.origin).resolve()
    owned = {Path(distribution.locate_file(item)).resolve() for item in distribution.files or ()}
    if not origin.is_relative_to(prefix) or origin not in owned:
        raise ValueError("Import is not owned by the installed distribution: " + name)
    module = importlib.import_module(name)
    if Path(module.__file__).resolve() != origin:
        raise ValueError("Imported module differs from the inspected origin: " + name)
    return module, origin.relative_to(prefix).as_posix()


def version_attribute(module, attribute: str, *, call: bool = False) -> dict:
    """Report absent native version APIs as unknown instead of inferring a version."""
    value = getattr(module, attribute, None)
    if value is None:
        return {"status": "unknown", "reason": "attribute-not-exposed"}
    if call:
        value = value()
    if isinstance(value, (tuple, list)) and all(isinstance(part, (int, str)) for part in value):
        value = ".".join(str(part) for part in value)
    if not isinstance(value, str) or not value:
        return {"status": "unknown", "reason": "version-value-not-exposed"}
    return {"status": "known", "value": value}


def file_sha256(path: Path) -> str:
    """Hash a file with bounded memory on every supported Python version."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def native_files(distribution, prefix: Path, artifact: dict | None) -> list[dict]:
    """Hash only metadata-owned native files confined to this interpreter prefix."""
    result, seen = [], set()
    for entry in distribution.files or ():
        if not re.search(r"\.(?:dll|pyd|dylib|so(?:\..+)?)$", str(entry), re.IGNORECASE):
            continue
        path = Path(distribution.locate_file(entry)).resolve()
        if not path.is_relative_to(prefix):
            raise ValueError("Native file escaped the interpreter prefix")
        relative = path.relative_to(prefix).as_posix()
        if relative in seen:
            raise ValueError("Duplicate native file ownership in distribution metadata")
        seen.add(relative)
        if not artifact or artifact.get("kind") != "wheel":
            raise ValueError("Native distribution lacks a reported wheel identity")
        result.append({"path": relative, "bytes": path.stat().st_size, "sha256": file_sha256(path),
                       "wheel_sha256": artifact["sha256"]})
    return sorted(result, key=lambda item: item["path"])


def source_identity(repository: Path) -> dict:
    """Describe repository identity without retaining paths, filenames or environment values."""
    try:
        revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository, check=True,
                                  capture_output=True, text=True, timeout=10).stdout.strip()
        status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=normal"], cwd=repository,
                                check=True, capture_output=True, text=True, timeout=10).stdout
        tracked = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=repository,
                                 check=True, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return {"status": "unknown", "reason": "git-identity-unavailable"}
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Unexpected Git revision identity")
    return {"status": "known", "revision": revision, "dirty": bool(status), "tracked_dirty": bool(tracked),
            "untracked_files_present": any(line.startswith("?? ") for line in status.splitlines()),
            "scope": "Overall state includes generated CI receipts; tracked_dirty distinguishes tracked source changes"}


def capture(report_path: Path, repository: Path) -> dict:
    """Collect the scientific test environment without invoking providers or native engines."""
    if not sys.flags.isolated or not sys.dont_write_bytecode or site.ENABLE_USER_SITE:
        raise ValueError("Invoke this helper with python -I -B")
    prefix = Path(sys.prefix).resolve()
    content = report_path.read_bytes()
    installed = installed_distributions(metadata.distributions())
    report = json.loads(content)
    reported = reconcile_report(report, installed, repository)
    _, stormhub_origin = verify_import("stormhub", installed["stormhub"], prefix)
    native_versions = {"python": {"openssl": {"status": "known", "value": ssl.OPENSSL_VERSION}}}
    native_origins = {}
    for name, attributes in NATIVE_VERSIONS.items():
        module, origin = verify_import(name, installed[name], prefix)
        native_origins[name] = origin
        native_versions[name] = {library: version_attribute(module, attribute) for library, attribute in attributes}
    native_versions["fiona"]["proj"] = version_attribute(
        importlib.import_module("fiona._env"), "get_proj_version_tuple", call=True
    )
    h5py, native_origins["h5py"] = verify_import("h5py", installed["h5py"], prefix)
    native_versions["h5py"] = {"hdf5": version_attribute(h5py.version, "hdf5_version")}
    inventory = []
    owned_native = set()
    for name, distribution in sorted(installed.items()):
        install = reported.get(name)
        roles = ["reported-runtime-or-test-dependency"]
        if name == "stormhub":
            roles = ["application", "runtime"]
        elif name == "pytest":
            roles = ["test"]
        elif not install:
            roles = ["unreported-bootstrap" if name in BOOTSTRAP else "unreported-preexisting"]
        if name in BOOTSTRAP and install:
            roles.append("bootstrap-capable")
        files = native_files(distribution, prefix, install["artifact"] if install else None)
        for item in files:
            if item["path"] in owned_native:
                raise ValueError("Native path is owned by multiple distributions")
            owned_native.add(item["path"])
        inventory.append({"name": name, "version": distribution.version, "roles": roles,
                          "pip_report": install, "native_files": files})
    after = installed_distributions(metadata.distributions())
    if {name: dist.version for name, dist in installed.items()} != {name: dist.version for name, dist in after.items()}:
        raise ValueError("Installed inventory changed during capture")
    return {
        "schema": "stormhub/scientific-ci-environment/1", "scope": "scientific-runtime-and-test-environment",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "install_report": {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content),
                           "format_version": report["version"], "pip_version": report.get("pip_version")},
        "python": {"version": sys.version.split()[0], "implementation": platform.python_implementation(),
                   "system": platform.system(), "machine": platform.machine(),
                   "isolated": True, "bytecode_disabled": True, "user_site": False},
        "source": source_identity(repository), "stormhub_import": stormhub_origin,
        "distributions": inventory, "native_versions": native_versions, "native_imports": native_origins,
        "unreported_bootstrap": sorted((set(installed) - set(reported)) & BOOTSTRAP),
        "unreported_preexisting": sorted(set(installed) - set(reported) - BOOTSTRAP),
        "role_scope": {"runtime": "StormHub with nwm extra", "test": "pytest",
                       "transitives": "Shared runtime/test transitives are not assigned an exclusive role",
                       "bootstrap": "pip/setuptools/wheel may predate the reported installation"},
        "limits": ["CI provenance only; not a deployment lock, release acceptance or security clearance.",
                   "Wheel hashes are from pip's report; native hashes describe actual installed package-owned files.",
                   "OS libraries, generated launchers and native engine installations are not inventoried.",
                   "Documentation, notebooks and isolated package-build environments are outside this receipt.",
                   "Native version imports do not qualify numerical behavior or engineering models."],
        "solver_executed": False, "jvm_started": False, "provider_acquisition": False,
        "security_clearance": False, "runtime_activation_authorized": False,
    }


def main() -> int:
    """Write one new receipt after all identity checks complete."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists; preserve the earlier receipt")
    try:
        result = capture(args.install_report, Path(__file__).resolve().parents[2])
        with args.output.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(result, stream, indent=2)
            stream.write("\n")
    except (OSError, ValueError, ImportError, KeyError) as error:
        detail = str(error) if isinstance(error, ValueError) else type(error).__name__
        print("Scientific environment capture failed: " + detail, file=sys.stderr)
        return 1
    print(json.dumps({"output": args.output.name, "distributions": len(result["distributions"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
