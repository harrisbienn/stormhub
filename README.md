# StormHub
[![CI](https://github.com/dewberry/stormhub/actions/workflows/ci.yaml/badge.svg?branch=main)](https://github.com/dewberry/stormhub/actions/workflows/ci.yaml)
[![Documentation Status](https://readthedocs.org/projects/stormhub/badge/?version=latest)](https://stormhub.readthedocs.io/en/latest/?badge=latest)
[![Release](https://github.com/dewberry/stormhub/actions/workflows/release.yaml/badge.svg)](https://github.com/dewberry/stormhub/actions/workflows/release.yaml)
[![PyPI version](https://badge.fury.io/py/stormhub.svg)](https://badge.fury.io/py/stormhub)


**StormHub** is an open-source Python library designed to access and process publicly available hydrometeorological data to create catalogs, metadata, and data products for hydrologic modeling. This project automates the generation of STAC catalogs from storm and stream gage data, enabling improved analysis and simulation for flood studies and stochastic storm transposition (SST). StormHub aims to follow the principles of **[FAIR](https://www.nature.com/articles/sdata201618) (Findable, Accessible, Interoperable, and Reusable)** practices, ensuring that all catalogs can be easily reproduced, shared, published, and integrated into broader workflows.

## Overview
StormHub consists of two primary modules, with a focus on storm data and stream gage metadata:

### 1. Storm Transposition Module
This module extends the work of [RainyDay2](https://her.cee.wisc.edu/rainyday/rainyday-users-guide/) developed by Daniel Wright's Hydroclimate Extremes Research group at the University of Wisconsin-Madison. It allows users to perform [stochastic storm transposition](https://www.sciencedirect.com/science/article/abs/pii/S0022169420302766) (SST) by systematically shifting a watershed over a predefined transposition region and summing precipitation from existing datasets.

**Key Features:**
- Uses the recently published **[AORC](https://registry.opendata.aws/noaa-nws-aorc/)** hourly 1km gridded precipitation dataset.
- Sums precipitation over a time slice (e.g., 72 hours).
- Generates a catalog ranking storms by mean precipitation over the transposition region.
- Filters storms that exceed a minimum precipitation threshold.
- Stores qualified storms as STAC items with metadata, including:
  - Storm statistics (e.g., total precipitation, duration).
  - Centroid location of the watershed at the point of maximum mean precipitation.
- Links to associated watershed and transposition region STAC items.
- Supports creation of **DSS files** (hourly gridded) for use in HEC-HMS for hydrologic modeling.

*Note on hec-dss*: To export gridded time series data from xarray to hecdss, [hec-dss-python](https://github.com/HydrologicEngineeringCenter/hec-dss-python) is required. Please visit the repo for details and installation instructions.

### 2. USGS Gage Catalog Module
This module creates a STAC catalog of USGS stream gages, including frequency analysis data and metadata notes providing a *moment in time* copy of historic observations.

The catalog stores USGS gage items with:
- Frequency data as assets.
- Metadata and plots supporting flood frequency analysis.
- Links to related datasets for direct comparison within SST workflows.

---

### STAC Server
Use `stormhub-server <directory>` to preview trusted local catalogs at
`http://127.0.0.1:5000`. Stop it with **Ctrl+C**. There is no HTTP shutdown or write
endpoint. Display names are HTML-escaped and links are URL-encoded. Resolved file,
directory, and index paths must remain inside the selected root; external symlinks
and Windows junctions are denied and omitted from listings. Internal links are
allowed. Keep the root and its ancestors writable only by trusted operators: this
check does not protect against a concurrent filesystem writer swapping links.

An explicit non-loopback bind requires both a host and `--allow-network`, for
example `stormhub-server <directory> 192.0.2.10 5000 --allow-network`. This exposes
an **unauthenticated** preview to that network. The preview is not a cloud model
library endpoint. A staff-only library needs its own authenticated, authorized,
read-only service behind private ingress and TLS. CORS permits the hosted
[Radiant Earth STAC Browser](https://radiantearth.github.io/stac-browser/); CORS is
browser policy, not access control. Serve only content you trust, including HTML.

StormHub also exposes `stormhub.publishing.publish_authenticated_item` as a
domain-neutral publication boundary. A caller supplies its own Item identity,
geometry, temporal extent, searchable properties, relationships, and
checksum-pinned assets. StormHub verifies local bytes and writes a portable
STAC tree without importing or reinterpreting the caller's domain contract.

Publication IDs must be single portable filename components: Windows device names,
drive/stream syntax, control characters, and leading/trailing whitespace or trailing
dots are rejected. Generated destinations and existing Collection write paths must
resolve inside the catalog directory, including through symlinks or junctions.
Keep that directory writable only by trusted publishers. The optional `item_path`
is an explicit trusted-caller override; do not expose it to library readers.

### FAIR Data Sharing and Publishing
StormHub facilitates FAIR data principles by enabling:
- Exporting catalogs as **zip files** for easy sharing and archiving.
- Publishing STAC items directly to a **STAC API**.
- Copying catalogs and metadata to **cloud blob stores** for scalable access and distribution.

## Installation
```bash
# Using pip
pip install stormhub

# From source: Clone the repository
git clone https://github.com/dewberry/stormhub.git

# Navigate to the project directory
cd stormhub

# Install the package
pip install -e .
```

## Usage
See the [User Guide](https://stormhub.readthedocs.io/en/latest/user_guide.html).

## Sources and References
- **AORC Dataset** - 1km hourly gridded precipitation data, available through NOAA.
- **RainyDay2** - Stochastic Storm Transposition framework by the [Hydroclimate Extremes Research Group](https://her.cee.wisc.edu/)
- **USGS Stream Gage Data** - Accessed via NWIS API.

## Output
- **STAC Catalogs** for storm and gage data.
- **DSS Files** for hydrologic modeling.
- JSON metadata files for integration with existing geospatial workflows.

## Attribution
This project builds on the work of Daniel Wright's [RainyDay2](https://her.cee.wisc.edu/rainyday/rainyday-users-guide/) and leverages publicly available datasets from NOAA and USGS.

## License
StormHub is licensed under the MIT License. See [LICENSE](LICENSE) for more information.

## CI and releases

`CI` runs on every pull request and main/dev push, including dependency-only and
workflow-only changes. It checks Python 3.10?3.12 on Linux and 3.12 on Windows.
The portable subset covers publication, scenario contracts, preview confinement,
and HTTP-client credential/redirect/proxy/streaming behavior. Only the preview
tests use sockets, on ephemeral loopback ports; no test calls an external service.
Package installation still needs the package index. Native DSS/GIS/HEC and large
model qualification remain separate gates.

Reproduce the portable check in a clean environment:

```bash
python -m pip install pip==26.2.1 pytest==9.1.1 packaging==26.3
python -m pip install --no-deps .
python .github/scripts/install_ci_dependencies.py
python -I -m pytest tests/test_authenticated_publisher.py tests/test_preview_server.py tests/test_requests_security.py tests/test_scenario_contract.py tests/test_scenario_publisher.py tests/test_scenario_response.py tests/test_scenario_stac.py
```

The CI dependency selector reads runtime pins from the installed package metadata;
it is not a substitute for a complete scientific environment installation.
Requests is pinned consistently to 2.34.2 in package, Conda, and documentation
requirements. Its upstream fixes cover
[netrc credential leakage](https://github.com/psf/requests/security/advisories/GHSA-9hjg-9r4m-mvj7)
and [archive extraction](https://github.com/psf/requests/security/advisories/GHSA-gc5v-m9x4-r6x2).
The Requests regression fixtures contain dummy credentials and use a fake HTTP
adapter, not a live download. Broader native dependency qualification is separate.

`Release` is now manual and accepts only `main`; leave `publish` false for a
validation-only run. It reruns the same CI workflow for the selected revision and
publishes only that run's distribution artifact after all matrix checks pass.
Build jobs have read-only repository tokens and no persisted checkout credentials.
Separate publication jobs use the `pypi` environment; PyPI uses short-lived OIDC,
and only the GitHub release job can write repository releases. PR jobs cannot
reach either publication job. Actions are commit-pinned and Dependabot proposes
reviewed updates.

Before the first release, an administrator must configure the PyPI trusted publisher
for this repository, `release.yaml`, and environment `pypi`; restrict that environment
to `main` with reviewer approval; and protect `main` with the CI unit matrix and
`Build tested distribution` checks. Configure the publisher before removing the
obsolete `PYPI_TOKEN` secret; the new workflow does not consume it. These settings
are not established by committing workflow YAML. The 2026-09-21 review found main
unprotected; no package was released as part of validation. See
[PyPI's setup instructions](https://docs.pypi.org/trusted-publishers/using-a-publisher/).

Ruff checks package, test, and CI-helper docstrings and critical syntax errors.
Formatting checks cover the rewritten preview and new security tests/helpers;
pre-existing formatting drift in 12 files and a notebook docstring violation are
not mixed into this security patch.
