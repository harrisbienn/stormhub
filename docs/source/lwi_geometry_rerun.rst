LWI Region 3 geometry rerun
===========================

Preparation recorded on 2026-09-24. Generation is tracked by
`StormHub #15 <https://github.com/harrisbienn/stormhub/issues/15>`_; adoption is
tracked by
`FloodForecast #105 <https://github.com/harrisbienn/floodforecast/issues/105>`_.
The operator supplied the replacement inputs on 2026-09-25. Creation reads
``configs/params-config.json``; population selects ``lwi-region3-geometry-v2``
and reads its frozen ``creation-settings.json`` snapshot.
The replacement base catalog is now present locally; its eight files passed
a read-only reuse check during the notebook refactor. No storm population or
DSS generation was run as part of the wiring/refactor checks.
Engineering review of the prepared footprint and derived valid transposition
region remains part of the pre-search workflow.

Supplied HUC8 inputs
--------------------

The configured inputs are Esri JSON, not GeoJSON despite the ``.json`` suffix:

* Watershed: ``data/lwi-region3/lwi_r3_huc8_domain.json``; 11 valid Polygon
  features in EPSG:4269 (NAD83); SHA-256
  ``f6e4be3308ab0a056975f53db983eacda868029c0b405baee9957586cf0a1d5d``.
* Climate region:
  ``data/lwi-region3/lwi_r3_huc8_climate_transposition_domain.json``;
  one valid Polygon in EPSG:4326 (WGS84); SHA-256
  ``f06a6640f8de7e43bc9359fab61f95205bae3ee75aacce9587b01d7d08c0c530``.

The creation notebook verifies these source hashes, reprojects to WGS84, and
unions all watershed features into one valid Polygon without simplification,
buffering, or repair. The climate region covers the entire prepared watershed.
Single-feature GeoJSON derivatives are written under the new catalog's
``inputs/`` directory; source files remain unchanged. The new domain IDs are
``lwi-r3-huc8-domain`` and ``lwi-r3-huc8-climate-transposition-domain``.

The local environment's GeoPandas bulk dissolve failed with Shapely 2.0.5 and
NumPy 2.4.6. Preparation uses pairwise geometric union, which passed against all
11 source features. This does not establish readiness of the remaining native
search/DSS stack; retain the representative export checks before full execution.

Both the watershed and transposition region have changed. The watershed is
the precipitation-averaging footprint and the region bounds the storm search.
Recompute statistics, storm selection, ranks, placements, and DSS products for
all three durations. Old ranks are not stable storm identities across catalogs.

Preserve the historical catalog
-------------------------------

Keep ``catalogs/lwi-region3/`` unchanged and available at its current path.
FloodForecast studies, campaigns, and historical evidence reference it directly.
The operator supplied ``catalogs/lwi-region3-deprecated-20260924.zip``; its name
does not redirect those references or authorize removal of the original folder.
Both ``catalogs/`` products and ``data/`` inputs are ignored by Git.

Archive verification on 2026-09-24 recorded:

* Archive size: 6,077,552,008 bytes.
* SHA-256:
  ``5d5a22abc5829dc066fb4ade7f2fae82ae4655a18686f330a0964717a59acf5e``.
* ZIP root: ``lwi-region3/``; 6,924 files with 6,780,130,995 uncompressed bytes
  and no duplicate file entry names.
* Each of 24, 48, and 72 hours contains 460 source and 460 target DSS files.
  All three authoritative ``<duration>hr-events/dss/dss-manifest.csv`` files
  are present; their rows record ``passed``.
* Every archived file was streamed through Python's ``zipfile`` CRC checking
  and SHA-256 hashing and matched its existing catalog counterpart. All 2,760
  DSS assets matched the manifests' SHA-256 multihashes, with no unresolved
  manifest assets or checksum failures.

These checks establish archive readability and byte agreement. They do not
rerun native DSS validation, prove engineering acceptance, test filesystem
restoration, or establish an independent backup. Preserve original external
GeoJSON inputs and generation settings separately where available; the archive
contains catalog geometry Items, but its configuration may point outside the
ZIP. If original inputs have already changed, record that provenance gap.

Before retiring any historical copy, produce a file-level path/size/SHA-256
inventory with counts and byte totals, copy to independent storage, verify it,
and test restoration into an empty isolated directory. Record storage owner,
restore location, and checksum results under the integration issue. Do not
extract the archive over either the historical catalog or the new generation.

Prepare the new catalog and inputs
----------------------------------

Use the StormHub scientific environment with its native ``hecdss`` dependency.
The portable CI environment alone does not qualify the full GIS/DSS workflow.
Record the actual generation revision and environment. The preparation baseline
was StormHub ``35db749b74b80b26d543f34d8030838e00732ec4``; local and remote main
matched. FloodForecast's clean component verification passed before branching.

1. Preserve the revised input files under new names and record their SHA-256,
   source, CRS, and engineering disposition. Review valid polygon geometry,
   watershed footprint, and the intended transposition region.
2. Verify the paths, hashes, and new watershed/region IDs in
   ``configs/params-config.json``. The creation notebook resolves those paths
   against the repository root and prepares the single-polygon inputs.
3. Creation derives the catalog ID from its editable configuration. Population
   selects ``CATALOG_ID`` independently and verifies the saved snapshot's ID.
   ``lwi-region3-geometry-v2`` is configured. Keep ``EXISTING="error"`` for
   fresh creation, or explicitly select ``"reuse"`` to verify and reopen this
   same generation. Conflicting inputs require a new catalog ID.
4. Create the catalog and inspect its watershed, transposition region, and
   derived valid transposition region. The valid region describes placements
   where the watershed can fit within the transposition region. Confirm it is
   nonempty and scientifically appropriate before the full search.

The resulting layout should keep independent generations::

   catalogs/
     lwi-region3/                         # historical, unchanged
     lwi-region3-deprecated-20260924.zip   # historical archive
     lwi-region3-geometry-v2/              # configured new generation

Freeze settings and rerun discovery
-----------------------------------

``configs/params-config.json`` supplies creation with these settings; its frozen
``catalogs/lwi-region3-geometry-v2/creation-settings.json`` snapshot supplies
population defaults.
They are inherited preparation defaults, not a new engineering approval:

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Setting
     - Current notebook value
   * - Search dates
     - 1979-02-01 through 2025-12-31
   * - Durations
     - Run separately for 24, 48, and 72 hours
   * - Retained events
     - 460 per duration
   * - Minimum precipitation
     - 2.5 inches
   * - Search step
     - 6 hours
   * - Windows processing
     - 8 workers; ``USE_THREADS=False``
   * - DSS products
     - Source and target; 1-km SHG; 5-km target buffer
   * - Source extraction region
     - ``use_valid_region=True``

The previous 24-hour config / 6-hour notebook discrepancy is resolved by retaining
the population notebook's 6-hour step in the creation config. Search methodology
and export choices remain subject to the pre-search review. Creation freezes a
``creation-settings.json`` in the new catalog. Population reads that snapshot
without consulting the editable config, which may now describe another catalog.
Select 24/48/72 hours through ``STORM_DURATION_HOURS`` in the
population notebook without changing the frozen config between durations.
Record all geometry/config hashes and export options in generation evidence.
``use_valid_region`` affects source DSS
coverage and must also be reviewed, particularly for the full HMS forcing
domain. The search watershed, HMS forcing domain, and RAS coverage domain are
separate concepts; a 5-km target buffer is not proof of model coverage.

For each fresh duration, run ``stormhub populate`` or the population notebook's
shared ``populate_catalog`` workflow with ``SMOKE_TEST=False``. Set
``RUN_DSS_EXPORT=False`` during notebook discovery so export does not start
before the new population is reviewed. The CLI keeps export separate:

.. code-block:: powershell

   stormhub populate catalogs/lwi-region3-geometry-v2 --duration 24 --dry-run
   stormhub populate catalogs/lwi-region3-geometry-v2 --duration 24

The local 72-hour collection already contains smoke-search Items and DSS.
Fresh population deliberately refuses that existing workspace. Prepare a new
catalog generation for its full search; do not silently expand or overwrite the
smoke results. The CLI reads ``creation-settings.json`` and records search scope
and domain hashes in ``<duration>hr-events/population-settings.json``.
Preserve the new ``storm-stats.csv``,
``ranked-storms.csv``, Items, and settings; check temporal search completeness
before accepting the rankings.

Do not reuse historical statistics, ranked tables, or numeric Item directories.
``stormhub resume`` and the notebook's ``resume_catalog`` only continue a recorded
full search with unchanged settings/domain hashes and partial statistics, before
Items or DSS are created. They refuse smoke searches and older unrecorded runs.
An older notebook checkpoint can instead be explicitly migrated with
``stormhub adopt-checkpoint`` after all writers stop and the operator confirms
its actual settings. Adoption retains compatible statistics, including same-input
smoke dates, and preserves the entire old collection before retiring its ranked
products and removing its root child link. Follow the user guide's stopped
checkpoint adoption procedure; never fabricate a normal population record for
unverified historical statistics.
``workflows/rebuild_ranked_items.py`` reranks existing statistics and
cannot perform a geometry rerun. The notebook's resume cell is an alternative
for an interrupted search, disabled by default with ``RUN_RESUME=False``;
it is not a required second full-search step. If a resumed
search changes rankings after Items were created, reconcile/rebuild those new
Items before export; never let stale rank-addressed folders select old events.

Validate and export DSS
-----------------------

After completing and reviewing a duration's population:

1. Set ``RUN_DSS_EXPORT=True`` and ``DSS_ITEM_IDS=["1"]``; run the export cell
   only. Explicitly select ``collection_id=f"{STORM_DURATION_HOURS}hr-events"``
   and ``DSS_OUTPUT_MODES=("source", "target")``. The API default is source-only.
2. Require both spatial and DSS record-count validations to report ``passed``.
   Inspect source/target placement, SHG-cell offsets, target footprint, hourly
   coverage, and units. Repeat this smoke for every duration.
3. Export the remaining Items. ``DSS_ITEM_IDS=None`` selects the whole
   collection. The shared workflow now refuses existing selected outputs unless
   ``OVERWRITE_DSS=True`` (CLI: ``--overwrite``) is explicit. To retain the
   successful smoke bytes, provide an explicit list of only unexported IDs.
4. Check the returned ``failed_count``, per-item statuses, and validation
   reports. Retry only failed/unexported IDs when preserving successful assets.
   The exporter removes and replaces selected DSS outputs; it is not an
   automatic skip-existing/resume mechanism.

The equivalent CLI smoke export is:

.. code-block:: powershell

   stormhub export-dss catalogs/lwi-region3-geometry-v2 --duration 24 --item-ids 1 --dry-run
   stormhub export-dss catalogs/lwi-region3-geometry-v2 --duration 24 --item-ids 1

Use ``--all-items`` only for an intentional whole-collection export. The three
existing 72-hour smoke targets can be inspected with ``--duration 72 --item-ids
1 2 3 --output-modes target --overwrite --dry-run`` without changing their bytes.

Each duration must deliver its authoritative
``<duration>hr-events/dss/dss-manifest.csv``, source/target STAC assets and
checksums, and per-item ``dss-validation`` reports. Reconcile unique ranks and
Item identities across ranked tables, Items, manifests, files, and any generated
centroid/GeoParquet indexes. Do not use an unrelated top-level manifest merely
because it has the same filename. Regenerate optional indexes from the new
population; normal-precipitation products remain optional.

The intended population is 460 targets and 460 sources per duration: 1,380
targets and 1,380 sources overall. Report shortages or failures explicitly;
never fill gaps with historical events or claim completion from file counts
alone. Recheck historical hashes after generation to establish that the old
catalog and archive remain unchanged.

Handoff and acceptance
----------------------

StormHub #15 owns generation evidence: approved geometry/config identities,
revision/environment, catalog and Item identities, all three manifests, asset
hashes, validation results, exact counts, and unresolved findings.
FloodForecast #105 owns the subsequent integration record under
``agent_tasks/cross-repo/``, affected study/profile references, refreshed
compatibility evidence, new response/campaign identities, representative
HMS/RAS qualification, and rollout/rollback decision. Do not change those
consumer bindings before the replacement forcing is accepted.

Merge owning component changes first; update ``components.lock.toml`` only
after those merges. Historical ``stormhub/scenario-run/2.2.0`` records remain
unchanged. New normalized responses use
``floodforecast/scenario-response-run/1.0.0`` and
``floodforecast/scenario-response-campaign/1.0``; no schema revision is proposed.
New forcing identities require new response specifications and ledgers, not
resumption or relabeling of historical campaigns.

FloodForecast acceptance includes focused tests, component verification,
applicable study doctors, and representative duration/profile checks through
package-owned HMS/RAS APIs. Keep execution, handoff, hydraulic QA/QC, and
publication dispositions separate and ``forecast_eligible = false``. Forecast
and diversity-feature approvals are not added prerequisites for deterministic
library generation. Component PRs track the parent; only the final accepted
integration PR closes FloodForecast #105.

Wiring validation on 2026-09-25
-------------------------------

Offline execution of the notebook preparation cells against the supplied files
passed source hash verification, WGS84 polygon preparation, climate coverage,
and a GeoJSON write/read round trip through ``HydroDomain``. Checks also passed
for source checksum rejection, destination overwrite refusal, shared population
defaults, frozen-config drift rejection, and syntax of all code cells. Outputs
from the prior catalog were cleared from both notebooks to avoid presenting
historical results as evidence for the new geometry.

``components verify`` reports the expected StormHub branch/revision mismatch
while this work is on the unmerged rerun branch; other component and historical
schema checks passed. Keep the lock unchanged until merge. AORC-derived valid
region creation, full storm discovery, DSS export, and HTML rendering are not
validated by these offline checks. The local environment has no Sphinx; run
``python -m sphinx -b html docs/source docs/build/html`` in the documentation
environment to check rendered documentation.

Notebook refactor validation on 2026-09-25
------------------------------------------

Reusable creation logic now lives in ``stormhub.met.catalog_setup``. The
notebook keeps configuration, inspection displays, package calls, and the
generated-file listing. See the user guide's catalog-creation section for
``EXISTING="error"`` versus ``"reuse"`` and the recommendation to stage and
back up any future same-path replacement rather than add a destructive boolean.

The setup and existing catalog test modules passed all 38 tests, using small
real geometries and a stubbed AORC availability boundary. Coverage includes
source mutation, CRS conversion, invalid/disconnected geometry, settings drift,
prepared-input tampering, external/missing domain links, failed-build retry,
and preservation of completed catalogs and event products. Ruff lint/format
and diff whitespace checks passed. A read-only reuse of the current local
base catalog verified all eight files' hashes and modification times unchanged.
No AORC discovery, DSS regeneration, replacement, or deletion of a catalog was
performed. The component-lock and HTML-build limitations above still apply.

Snapshot naming and authority
-----------------------------

The catalog-local ``params-config.json`` snapshot was renamed to
``creation-settings.json`` to distinguish it from the editable file under
``configs/``. The existing LWI v2 snapshot was migrated without changing its
contents; its SHA-256 remains
``335ad1cd7768f9b127ce7b9b7fef6789b3e48be85d3e226d27f55539e4bebf2a``.
Keep ``configs/params-config.json`` for new creation and keep the catalog snapshot
for existing-generation provenance and population. The runtime
``lwi-r3-config.json`` still binds prepared geometry paths/checksums. Historical
``lwi-region3/`` and its ZIP are unchanged by this migration.

Recovering after discovery and Item-rendering failures
------------------------------------------------------

A stopped process is not evidence of a complete catalog. Reconcile every
expected search start against ``storm-stats.csv`` before accepting rankings,
then check the selected ranks against actual Item JSON, thumbnails, and the
collection's links. Empty numeric directories are failed attempts, not Items.
The normal ``resume`` command deliberately refuses these directories.

Hidden Windows workers previously attempted to create a Tk desktop window
when rendering AORC thumbnails. Thumbnail generation now uses a Matplotlib Agg
canvas directly, without changing the notebook's interactive backend. Check a
real Item and representative DSS pair before rebuilding the remaining Items.

Preserve statistics, rankings, catalog links, logs, and a checksum-complete
backup before recovery. Once search coverage is reconciled, the package's
``rebuild_ranked_collection_items`` API can regenerate Items from the retained
statistics without repeating discovery; it quarantines existing numeric
directories by default. Verify every intended Item before accepting the
resulting collection or exporting its DSS. Keep the checkpoint-adoption receipt
at the path referenced by ``population-settings.json``.

The 72-hour search on 2026-09-27 retained 68,532 of 68,541 requested starts.
The nine absent starts, 2025-12-29 00:00 through 2025-12-31 00:00 UTC at six-hour
steps, require 2026 data. On 2026-09-28 NOAA's 2025 Zarr time axis ended at
2025-12-31 23:00 UTC and the 2026 metadata returned HTTP 404. Do not silently
shorten those storms or edit frozen settings to hide the missing dates. Record
the unavailable windows and an explicit operator disposition, or defer
finalization until data is available. A ranking from fewer requested windows
must retain that qualification even if it yields all 460 selected events.

For this 72-hour recovery, the operator accepted those nine unavailable windows
as exclusions on 2026-09-28. The accepted search population is 68,532 available
windows out of the original 68,541 requested starts. The first excluded start
is 2025-12-29 00:00 UTC, the last is 2025-12-31 00:00 UTC, and all nine are on
the six-hour grid. Frozen creation/population settings and retained statistics
remain unchanged. Catalog-local ``72hr-events/search-coverage.json`` records
each excluded start/end, the upstream availability evidence, approval, and
input/statistics hashes; expose it as a collection metadata asset when the
recovered collection is finalized. Describe this as complete with documented
exclusions, not complete coverage of the original request. This disposition
does not automatically approve exclusions for the 24- or 48-hour searches.

Target precipitation coverage at placement-domain edges
-------------------------------------------------------

The valid transposition region limits storm placement; it must not truncate
the precipitation needed to fill a target watershed. The initial 72-hour DSS
batch exposed this at ranks 5 and 16, whose translated target grids stopped
about 4.47 km short of the western watershed bound.

For target exports, retrieval now includes the inverse-translated target
footprint, its configured buffer, and two output-grid cells of padding for
spatial selection/reprojection, in addition to the configured retrieval AOI.
The inverse uses the same snapped SHG translation applied to the data. Search
statistics, ranks, storm-placement geometry, and translation offsets are
unchanged. The enlarged precipitation footprint may change the source grid
extent and reprojection alignment; preserve and replace the complete affected
source/target pair together so its source-to-target evidence stays consistent.

Spatial validation requires finite, non-nodata values in every raster cell
touching the watershed at every timestep, as well as envelope coverage and
preserved source values. A bounding box alone cannot establish coverage.
Back up affected outputs and metadata before explicit replacement; retain
already accepted pairs only after checking their actual watershed coverage.

Readback of all 72 records in each of the first 20 targets found affected ranks
3, 4, 5, 6, 7, 8, 9, 12, 13, 14, 16, and 17. The other eight pairs are retained.
Export validation now also checks the actual reopened DSS grids using their
stored origin and cell size; finite watershed coverage is required after
serialization as well as before writing. Zero rainfall remains valid data.
The 12 affected pairs and their previous metadata are preserved in the local
coverage-repair backup before selective replacement. Subsequent exports record
the enlarged retrieval method and padding in their validation evidence.

Approved 72-hour event-population exclusions
--------------------------------------------

This section records the 2026-09-28 disposition. The authorized backfills below
supersede its 458-event count and no-backfill restriction; the exclusions remain.

On 2026-09-28 the operator approved excluding original ranks 193 and 219 and
accepting a 458-event 72-hour population. This supersedes the earlier approval
for rank 193 alone and a conditional 459-event population.

Rank 193's 1998-12-10 00:00 through 1998-12-13 00:00 UTC storm has upstream
AORC nodata at the final hour. Native DSS readback found 1,681 missing watershed cells;
a direct NOAA Zarr probe confirmed the missing source value. Rank 219's
1986-11-07 00:00 through 1986-11-10 00:00 UTC storm has missing watershed values
in 25 hourly records, from 1986-11-09 00:00 through 1986-11-10 00:00 UTC.
Native readback found 150,725 missing cell-hour values; direct NOAA Zarr probes
confirmed upstream nodata at an affected source location. Both target
files contain 72 records and cover the watershed envelope. Repeating export
with the same upstream data cannot fill these gaps; zero rainfall must not be
substituted for missing data. These event exclusions are distinct from the nine
excluded search starts requiring unavailable 2026 data.

The decision is recorded in ``72hr-events/population-disposition.json``, linked
as collection metadata. After export finished, both failed Items and their four
DSS files were removed from the active collection and preserved under
``_recovery/20260928-72hr/excluded-r193-r219/``. Before moving them, the
``before-exclusion.zip`` backup was verified against a SHA-256 inventory of
945 metadata/evidence/output files totaling 23,108,035 uncompressed bytes.
Its SHA-256 is
``0c87ffff679c0ba0ba743467ace12950d450c7db2c774cd13735be9a4b1c6959``.
This is a same-disk recovery archive, not an independent backup.

The finalized active population contains 458 source/target pairs (916 DSS
assets), with original rank IDs and gaps at 193 and 219. The collection's
``ranked_storms`` asset points to ``accepted-ranked-storms.csv``;
``search-ranked-storms`` retains the original ``ranked-storms.csv``. Statistics,
the original ranking, frozen creation/population settings, and search-coverage
receipt remain byte-for-byte unchanged. No backfill, renumbering, or imputation
was performed.

Final reconciliation verified all 916 retained DSS checksums, four quarantined
DSS checksums, 916 passing manifest rows with no orphaned DSS, and 458 Items in
the collection and both geographic indexes. Native coverage evidence includes
the separate readback audit for the eight retained initial pairs. All 1,841
local collection/Item asset links resolve within the catalog with portable
relative paths. Terminal status is ``complete_with_documented_exclusions`` in
``_recovery/20260928-72hr/run-status.json``; the exclusion directory contains
``disposition-receipt.json`` and ``catalog-link-verification.json``.

Additional event failures require a separate disposition. These approvals apply
only to the 72-hour population and do not constitute HMS/RAS engineering
qualification or approval to adopt the new forcing in FloodForecast.

Validated backfills and current population
------------------------------------------

On 2026-10-02 the operator authorized selecting the top 460 temporally filtered
24- and 48-hour candidates without a rainfall cutoff, and backfilling the two
excluded 72-hour events. The original search statistics, threshold-based
rankings, and frozen population settings remain preserved. Effective selection
is recorded in each collection's ``selection-disposition.json`` and linked
``ranked_storms`` asset. The 72-hour population includes ranks 461 and 462,
retaining gaps at excluded ranks 193 and 219.

On 2026-10-05 the operator authorized replacing failed 48-hour rank 210 with the
next eligible candidate. Rank 210 (1986-11-07 through 1986-11-09 UTC) contained
48 records but had 6,947 missing watershed cells in one record. Rank 461,
2011-05-19 06:00 through 2011-05-21 06:00 UTC, passed source record validation
and native target readback with all 48 records and zero missing watershed
cells. Original rank IDs are retained; 461 is not relabeled as 210.

The failed Item, validation evidence, and DSS pair are preserved under
``_runs/20261005-48hr-backfill/excluded-210/``. The neighboring
``before-replacement/`` archive was verified against a file-size and SHA-256
inventory before moving the failed assets. All other 918 active 48-hour DSS
files were verified unchanged. The collection links, selected ranking,
geographic indexes, selection disposition, and DSS manifest were regenerated.
Reconciliation records 460 passing pairs, 920 manifest rows, and no orphan DSS.

The active catalog now contains 460 validated target events per duration.
The refreshed zonal analysis is under
``_analysis/20261005-target-zonal-backfilled/``; its CSV dataset and workbook
supersede the earlier analysis containing failed 48-hour rank 210. Retain the
earlier analysis as historical evidence. Downstream consumers should resolve
Items and checksums from the current collection/manifest rather than assume
rank IDs are contiguous or reuse an old exported inventory.

Backfilling does not fill missing search windows requiring unavailable 2026
AORC data. The 24- and 48-hour search receipts retain those coverage
qualifications. DSS validation and zonal completeness do not constitute
HMS/RAS engineering acceptance or authorize changing FloodForecast model or
campaign bindings.
