"""Check scientific provenance boundaries without importing scientific dependencies."""

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "scientific_environment", Path(__file__).resolve().parents[1] / ".github/scripts/record_scientific_environment.py"
)
environment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(environment)
CHECKSUM = "a" * 64


class Distribution:
    """Represent only metadata and filesystem ownership used by the helper."""

    def __init__(self, name, version="1.0", root=None, files=(), editable=False):
        """Supply explicit metadata without importing or installing a distribution."""
        self.metadata = {"Name": name}
        self.version = version
        self.root = root
        self.files = files
        self.editable = editable

    def locate_file(self, name):
        """Resolve a package-relative path against the fake installation."""
        return self.root / name

    def read_text(self, name):
        """Return a minimal direct installation record."""
        return json.dumps({"dir_info": {"editable": self.editable}})


def wheel(url="https://files.pythonhosted.org/packages/example-1.0-py3-none-any.whl"):
    """Create a minimal pip artifact identity."""
    return {"url": url, "archive_info": {"hashes": {"sha256": CHECKSUM}}}


def report(repository):
    """Describe a local application and a reported test wheel."""
    return {"version": "1", "install": [
        {"metadata": {"name": "StormHub", "version": "1.0"}, "requested": True,
         "download_info": {"url": repository.as_uri(), "dir_info": {}}},
        {"metadata": {"name": "pytest", "version": "1.0"}, "requested": True, "download_info": wheel()},
    ]}


def test_report_binds_installed_versions_and_distinguishes_local_source(tmp_path):
    """Retain wheel digests without inventing a hash for a local source build."""
    installed = {name: Distribution(name) for name in ("stormhub", "pytest")}
    result = environment.reconcile_report(report(tmp_path), installed, tmp_path)
    assert result["pytest"]["artifact"]["sha256"] == CHECKSUM
    assert result["stormhub"]["artifact"]["kind"] == "local-project"
    assert result["stormhub"]["artifact"]["wheel_sha256"] is None
    assert str(tmp_path) not in json.dumps(result)


def test_report_rejects_installed_version_mismatch(tmp_path):
    """An install plan cannot authenticate a different current environment."""
    installed = {"stormhub": Distribution("stormhub"), "pytest": Distribution("pytest", "2.0")}
    with pytest.raises(ValueError, match="version differs"):
        environment.reconcile_report(report(tmp_path), installed, tmp_path)


def test_installed_canonical_alias_collision_is_rejected():
    """Duplicate metadata cannot hide behind punctuation and case differences."""
    with pytest.raises(ValueError, match="Duplicate normalized installed"):
        environment.installed_distributions([Distribution("Package.Name"), Distribution("package_name")])


def test_report_canonical_alias_collision_is_rejected(tmp_path):
    """A repeated report entry cannot overwrite another spelling of the same project."""
    value = report(tmp_path)
    value["install"].append({**value["install"][1], "metadata": {"name": "PyTest", "version": "1.0"}})
    with pytest.raises(ValueError, match="Duplicate normalized pip-report"):
        environment.reconcile_report(value, {name: Distribution(name) for name in ("stormhub", "pytest")}, tmp_path)


def test_artifact_query_and_fragment_are_not_retained(tmp_path):
    """Credentials in URL query fields must not leak into committed provenance."""
    value = wheel("https://files.pythonhosted.org/example-1.0-py3-none-any.whl?token=secret-sentinel#private")
    result = environment.artifact_identity(value, "example", tmp_path)
    assert result["url"] == "https://files.pythonhosted.org/example-1.0-py3-none-any.whl"
    assert "secret-sentinel" not in json.dumps(result)


@pytest.mark.parametrize("url", [
    "https://user:secret-sentinel@files.pythonhosted.org/example-1.0-py3-none-any.whl",
    "https://foreign.example/example-1.0-py3-none-any.whl?token=secret-sentinel",
    "http://files.pythonhosted.org/example-1.0-py3-none-any.whl?token=secret-sentinel",
    "https://files.pythonhosted.org:secret-sentinel/example-1.0-py3-none-any.whl",
])
def test_untrusted_artifact_url_is_rejected_without_echoing_secrets(tmp_path, url):
    """Reject unsupported artifact origins with messages safe for CI logs."""
    with pytest.raises(ValueError) as caught:
        environment.artifact_identity(wheel(url), "example", tmp_path)
    assert "secret-sentinel" not in str(caught.value)


def test_report_rejects_foreign_local_source(tmp_path):
    """Local artifact provenance may identify only this StormHub source tree."""
    value = {"url": (tmp_path / "elsewhere").as_uri(), "dir_info": {}}
    with pytest.raises(ValueError, match="local StormHub"):
        environment.artifact_identity(value, "stormhub", tmp_path)


def test_import_rejects_editable_before_loading_code(tmp_path):
    """Editable metadata cannot be presented as an installed-wheel import."""
    with pytest.raises(ValueError, match="Editable"):
        environment.verify_import("stormhub", Distribution("stormhub", root=tmp_path, editable=True), tmp_path)


def test_import_rejects_ambient_origin_before_loading_code(tmp_path, monkeypatch):
    """A checkout or roaming import must not satisfy the installed identity check."""
    prefix = tmp_path / "environment"
    distribution = Distribution("stormhub", root=prefix / "site-packages", files=["stormhub/__init__.py"])
    monkeypatch.setattr(environment.importlib.util, "find_spec", lambda name: SimpleNamespace(origin=tmp_path / "__init__.py"))
    with pytest.raises(ValueError, match="not owned"):
        environment.verify_import("stormhub", distribution, prefix)


@pytest.mark.parametrize("filename", ["module.dll", "module.pyd", "module.so", "module.so.3.2", "module.dylib"])
def test_native_inventory_hashes_actual_owned_files_and_binds_wheel(tmp_path, filename):
    """Native identity comes from installed bytes and stays linked to its reported wheel."""
    prefix = tmp_path / "environment"
    library = prefix / "site-packages"
    library.mkdir(parents=True)
    (library / filename).write_bytes(b"small synthetic native-file bytes")
    (library / "ignored.txt").write_bytes(b"not a native file")
    distribution = Distribution("example", root=library, files=[filename, "ignored.txt"])
    files = environment.native_files(distribution, prefix, {"kind": "wheel", "sha256": CHECKSUM})
    assert files == [{"path": "site-packages/" + filename, "bytes": 33,
                      "sha256": hashlib.sha256(b"small synthetic native-file bytes").hexdigest(),
                      "wheel_sha256": CHECKSUM}]


def test_native_inventory_refuses_metadata_path_escape(tmp_path):
    """Distribution ownership alone cannot authorize hashing outside the interpreter."""
    prefix = tmp_path / "environment"
    distribution = Distribution("example", root=prefix / "site-packages", files=["../../outside.dll"])
    with pytest.raises(ValueError, match="escaped"):
        environment.native_files(distribution, prefix, {"kind": "wheel", "sha256": CHECKSUM})


def test_native_inventory_requires_reported_wheel_identity(tmp_path):
    """An unreported native installation cannot be silently linked to another artifact."""
    distribution = Distribution("example", root=tmp_path, files=["native.dll"])
    with pytest.raises(ValueError, match="lacks a reported wheel"):
        environment.native_files(distribution, tmp_path, None)


def test_absent_native_version_remains_explicitly_unknown():
    """Missing library-version attributes must not be substituted with package versions."""
    assert environment.version_attribute(SimpleNamespace(__version__="9.9"), "__gdal_version__") == {
        "status": "unknown", "reason": "attribute-not-exposed"
    }


def test_native_tuple_probe_records_actual_version():
    """Use the native version getter rather than inferring PROJ from Fiona's release."""
    module = SimpleNamespace(get_proj_version_tuple=lambda: (9, 5, 1))
    assert environment.version_attribute(module, "get_proj_version_tuple", call=True) == {
        "status": "known", "value": "9.5.1"
    }


def test_capture_refuses_shared_interpreter_before_reading_report(monkeypatch):
    """Runner-preinstalled packages must not silently satisfy scientific qualification."""
    monkeypatch.setattr(environment, "sys", SimpleNamespace(
        prefix="base", base_prefix="base", flags=SimpleNamespace(isolated=True), dont_write_bytecode=True
    ))
    monkeypatch.setattr(environment, "site", SimpleNamespace(ENABLE_USER_SITE=False))
    with pytest.raises(ValueError, match="fresh virtual environment"):
        environment.capture(Path("unused-report.json"), Path("unused-repository"))
