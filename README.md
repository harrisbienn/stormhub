# StormHub
[![CI](https://github.com/dewberry/stormhub/actions/workflows/ci.yaml/badge.svg?branch=main)](https://github.com/dewberry/stormhub/actions/workflows/ci.yaml)
[![Documentation Status](https://readthedocs.org/projects/stormhub/badge/?version=latest)](https://stormhub.readthedocs.io/en/latest/?badge=latest)
[![Release](https://github.com/dewberry/stormhub/actions/workflows/release.yaml/badge.svg)](https://github.com/dewberry/stormhub/actions/workflows/release.yaml)
[![PyPI version](https://badge.fury.io/py/stormhub.svg)](https://badge.fury.io/py/stormhub)


**StormHub** is an open-source Python library designed to access and process publicly available hydrometeorological data to create catalogs, metadata, and data products for hydrologic modeling. This project automates the generation of STAC catalogs from storm and stream gage data, enabling improved analysis and simulation for flood studies and stochastic storm transposition (SST). StormHub aims to follow the principles of **[FAIR](https://www.nature.com/articles/sdata201618) (Findable, Accessible, Interoperable, and Reusable)** practices, ensuring that all catalogs can be easily reproduced, shared, published, and integrated into broader workflows.

## Overview

The [rainfall feature contract](docs/source/precipitation_features.rst) provides
shared, area-weighted descriptors for target DSS and normalized incoming rainfall,
with authenticated source identity and explicit missingness.

The [NWM channel boundary](docs/source/nwm_channel.rst) provides bounded,
replayable analysis and short-range streamflow extracts for selected reaches.
Spatial equivalence and scientific use require a separately reviewed crosswalk.
The [NWM state boundary](docs/source/nwm_state.rst) adds bounded land wetness and
reservoir exports with explicit spatial coverage and unknown datum/state equivalence.

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
endpoint. Directories containing `catalog.json` or `collection.json` show an
**Open in STAC Browser** link for browsing from the computer running the server.
Restart an already-running server after updating StormHub to load this change.
Display names are HTML-escaped and links are URL-encoded. Resolved file,
directory, and index paths must remain inside the selected root; external symlinks
and Windows junctions are denied and omitted from listings. Internal links are
allowed. Keep the root and its ancestors writable only by trusted operators: this
check does not protect against a concurrent filesystem writer swapping links.

An explicit non-loopback bind requires both a host and `--allow-network`, for
example `stormhub-server <directory> 192.0.2.10 5000 --allow-network`. This exposes
an **unauthenticated** preview to that network. The preview is not a cloud model
library endpoint. A staff-only library needs its own authenticated, authorized,
read-only service behind private ingress and TLS. CORS permits the hosted
[STAC Browser](https://browser.moregeo.it) and its legacy Radiant Earth origin; CORS is
browser policy, not access control. Serve only content you trust, including HTML.

If STAC Browser reports that `http://127.0.0.1:5000/catalog.json` cannot be
loaded, first open that URL directly to check the server and selected directory.
The old Radiant Earth viewer now redirects to `browser.moregeo.it`; restart
StormHub after updating so its CORS headers allow the current viewer origin.
Reopen **Open in STAC Browser** from the local directory listing. If your browser
requests local-network access for `browser.moregeo.it`, allow it to read this
local preview; a previously denied permission can be changed in the site's
browser settings.

StormHub also exposes `stormhub.publishing.publish_authenticated_item` as a
domain-neutral publication boundary. A caller supplies its own Item identity,
geometry, temporal extent, searchable properties, relationships, and
checksum-pinned assets. StormHub verifies local bytes and writes a portable
STAC tree without importing or reinterpreting the caller's domain contract.

On Windows, local reference and asset reads support extended-length paths while
serialized STAC hrefs stay relative. Supply ordinary absolute paths to the API;
Windows device prefixes belong only at native file-I/O boundaries. Keep catalog
and Item destinations short enough for PySTAC's own filesystem I/O.
Use a mapped drive for Windows network catalogs. The publisher preserves that
logical namespace while checking physical resolved containment and reading long
asset paths. PySTAC 1.x cannot reliably reload relative links rooted at a UNC
host, so direct UNC catalog publication fails explicitly before writing. Native
asset reads still support UNC paths. Callers must preserve the mapped-drive
catalog path instead of resolving it to UNC before invoking the publisher.

Publication IDs must be single portable filename components: Windows device names,
drive/stream syntax, control characters, and leading/trailing whitespace or trailing
dots are rejected. Generated destinations and existing Collection write paths must
resolve inside the catalog directory, including through symlinks or junctions.
Keep that directory writable only by trusted publishers. The optional `item_path`
is an explicit trusted-caller override; do not expose it to library readers.

The generic publisher serializes cooperating writers with a persistent OS lock
and reloads the saved catalog inside that lock. Unsaved edits to a caller's
Catalog handle are not publication input. It stages and flushes complete JSON,
then publishes the immutable Item before atomically replacing the Collection and
root index. Readers never follow a newly written link to partial JSON. Identical
retries preserve accepted Item bytes and repair interrupted index membership;
different content under an existing identity fails. A complete unindexed
Collection is recovered before another Item is added.

All writers to a namespace must use this protocol. Historical scenario publishers,
manual PySTAC writes and external synchronization tools do not acquire its lock;
stop them before generic publication. Lock contention fails after 30 seconds and
can be retried; process death releases the OS lock. Do not delete lock files.
Atomic immutable publication requires same-filesystem hard links and replacement
semantics, qualified here on local NTFS. Qualify network/mapped-drive storage
separately; unsupported operations fail closed. Staging files use unique
`.stormhub-*.tmp` names. Abrupt process death can leave them behind; they are
never accepted records and must only be removed during a stopped-writer cleanup.
Existing partial or conflicting final files are preserved for explicit operator
quarantine, not automatically overwritten. Flushed file writes and atomic rename
protect process-interruption recovery; Windows power-loss durability and storage
recovery remain deployment qualification.

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

Use a fresh virtual environment and the dependencies declared in `pyproject.toml`;
add `.[nwm]` for the native NetCDF/HDF5 research readers. The old `env.yaml` is a
retired notebook snapshot whose versions disagree with package metadata. It is
retained as historical provenance, not a second supported installation recipe.
CI resolves these package requirements in disposable environments; its dependency
receipt is not a deployment lock or approval to replace an accepted study runtime.

### Scientific dependency qualification

Scientific CI runs the discovered small-fixture suite on Windows and Linux with
Python 3.12. It requires the native backends before testing, including the real
DSS, GRIB and NetCDF/HDF5 readers; missing packages must not turn these checks
into successful skips. Geometry and consolidated Zarr fixtures also exercise
Fiona/GeoPandas and the fsspec/S3Map interface without live S3 requests. Run the
same checks in a fresh environment:

```bash
python -m pip install --report scientific-install.json ".[nwm]" pytest==9.1.1
python -m pip check
python -I -m pytest -q -rs --strict-markers tests -m "not requires_network and not requires_hec and not large_assets"
python -I -B .github/scripts/record_scientific_environment.py --install-report scientific-install.json --output scientific-environment.json
```

Use fresh report destinations and `MPLBACKEND=Agg` for headless validation. CI
retains the installation report, resolved versions, native-library inventory and
JUnit results for 90 days. The inventory distinguishes installed runtime/test
packages, unreported bootstrap tools and the locally built StormHub source.
Isolated build and documentation environments are outside that inventory.
Dependency wheel digests and actual native-file identities are evidence for
review; they do not establish that every bundled native library is vulnerability
free or produce a self-contained deployment lock.

The November 2026 refresh under
[StormHub #34](https://github.com/harrisbienn/stormhub/issues/34) keeps the existing
Shapely, Rasterio and Zarr lines while updating the Fiona, GeoPandas and S3 stack
declarations. A fresh resolver may also choose newer transitive dependencies;
its exact test environment is distinct from a retained deployment candidate.
The legacy `env.yaml` remains historical. Only the two affected Fiona/GeoPandas
pins in the separate documentation recipe are synchronized; the broader
[documentation review](https://github.com/harrisbienn/stormhub/issues/30) remains
separate. Native/input applicability and deployment adoption are tracked under
[FloodForecast #98](https://github.com/harrisbienn/floodforecast/issues/98).
Component changes must merge before FloodForecast changes its source pin or
reconstructs an integration candidate. Fixture success does not authorize HEC
execution, provider acquisition, scientific acceptance or runtime activation.

## Usage
See the [User Guide](https://stormhub.readthedocs.io/en/latest/user_guide.html).

Catalog population is available through `stormhub populate`, `stormhub resume`,
`stormhub export-dss`, and `stormhub adopt-checkpoint` (also `python -m stormhub`). After updating an editable
checkout, run `python -m pip install --no-deps -e .` to register the new command.

```powershell
# Inspect the plan before starting a fresh duration search.
stormhub populate catalogs/lwi-region3-geometry-v2 --duration 24 --dry-run
# Export only selected Items after reviewing a completed collection.
stormhub export-dss catalogs/lwi-region3-geometry-v2 --duration 24 --item-ids 1 --dry-run
```

Remove `--dry-run` to execute. These commands read the selected catalog's frozen
`creation-settings.json`. Population refuses an existing duration directory;
resume requires matching saved search provenance and no existing Items or DSS.
Older notebook checkpoints can be migrated with `adopt-checkpoint` after stopping
all writers and confirming their scientific settings. It verifies a complete
collection backup, retains statistics, and retires stale ranked products before
enabling resume; `--dry-run` provides the fingerprint required to apply it.
Export requires `--item-ids ...` or `--all-items`, and refuses existing selected
outputs unless `--overwrite` is explicit. See the
[CLI workflow and limitations](docs/source/user_guide.rst#catalog-command-line-workflow).

For the LWI Region 3 geometry revision, follow the
[catalog rerun runbook](docs/source/lwi_geometry_rerun.rst). It records the
2026-09-24 archive verification and the fresh search, DSS validation, and
FloodForecast handoff steps. Build the replacement in a new catalog directory;
existing catalog paths remain historical references.

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
Package installation still needs the package index.

The `Scientific contracts` matrix runs Python 3.12 on Linux and Windows with the
full package and `nwm` dependencies. It discovers all tests under `tests/`, including
catalog setup/population/checkpoint recovery, source/target DSS export and coverage,
headless thumbnails, and the portable security suites. Tiny real DSS, GRIB,
geometry and NetCDF/HDF5 fixtures exercise the native readers without installed
HEC software or external provider calls. Required backend imports fail the job
before pytest if a native dependency is missing; optional imports cannot silently
turn this coverage into skips. Test results and resolved dependencies are retained
as workflow artifacts. The distribution build and manual release depend on both
the portable and scientific matrices passing.

Tests needing an external service, an installed HEC application, or large/untracked
assets must be marked `requires_network`, `requires_hec`, or `large_assets`,
respectively. Routine scientific CI excludes only these explicit gates, rather
than maintaining a second list of approved test modules. Such qualification remains
separate; a small-fixture pass does not establish engineering model acceptance.

Reproduce the portable check in a clean environment:

```bash
python -m pip install pip==26.2.1 pytest==9.1.1 packaging==26.3
python -m pip install --no-deps .
python .github/scripts/install_ci_dependencies.py
python -I -m pytest -q tests/test_authenticated_publisher.py tests/test_preview_server.py \
  tests/test_requests_security.py tests/test_scenario_contract.py tests/test_scenario_publisher.py \
  tests/test_scenario_response.py tests/test_scenario_stac.py tests/test_precipitation_features.py \
  tests/test_hrrr_acquisition.py tests/test_mrms_precipitation.py tests/test_nwm_channel.py \
  tests/test_nwm_state.py tests/test_publication_recovery.py tests/test_provider_recovery.py
```

Reproduce the scientific job in a separate clean Python 3.12 environment:

```bash
python -m pip install pip==26.2.1
python -m pip install ".[nwm]" pytest==9.1.1
python -m pip check
python -I -c "import hecdss, h5netcdf, h5py, numpy, pyproj, rasterio, geopandas, rioxarray; import stormhub.met.storm_catalog"
python -I -m pytest -q -rs --strict-markers tests -m "not requires_network and not requires_hec and not large_assets"
```

The CI dependency selector reads runtime pins from the installed package metadata;
it is not a substitute for a complete scientific environment installation.
Requests is pinned consistently to 2.34.2 in package metadata, documentation
requirements, and the historical Conda snapshot. Its upstream fixes cover
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
to `main` with reviewer approval; and protect `main` with the CI unit matrix,
both `Scientific contracts` checks, and `Build tested distribution`. Configure the
publisher before removing the obsolete `PYPI_TOKEN` secret; the new workflow does
not consume it. These settings
are not established by committing workflow YAML. The 2026-09-21 review found main
unprotected; no package was released as part of validation. See
[PyPI's setup instructions](https://docs.pypi.org/trusted-publishers/using-a-publisher/).

Ruff checks package, test, and CI-helper docstrings and critical syntax errors.
Formatting checks cover the rewritten preview and new security tests/helpers;
pre-existing formatting drift in 12 files and a notebook docstring violation are
not mixed into this security patch.
