LWI Region 3 geometry rerun
==========================

Preparation recorded on 2026-09-24. Generation is tracked by
`StormHub #15 <https://github.com/harrisbienn/stormhub/issues/15>`_; adoption is
tracked by
`FloodForecast #105 <https://github.com/harrisbienn/floodforecast/issues/105>`_.
No replacement catalog has been generated or accepted by this documentation
change. Updated geometry paths, hashes, and engineering disposition remain to
be recorded in the issue before execution.

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
---------------------------------

Use the StormHub scientific environment with its native ``hecdss`` dependency.
The portable CI environment alone does not qualify the full GIS/DSS workflow.
Record the actual generation revision and environment. The preparation baseline
was StormHub ``35db749b74b80b26d543f34d8030838e00732ec4``; local and remote main
matched. FloodForecast's clean component verification passed before branching.

1. Preserve the revised input files under new names and record their SHA-256,
   source, CRS, and engineering disposition. Review valid polygon geometry,
   watershed footprint, and the intended transposition region.
2. In ``notebooks/catalog_creation.ipynb``, set ``WATERSHED_PATH`` and
   ``TRANSPOSITION_REGION_PATH`` to those inputs. Use new watershed and region
   IDs in the generated configuration; do not reuse historical IDs for changed
   geometry.
3. Set a fresh ``CATALOG_ID`` in both creation and population notebooks.
   ``lwi-region3-geometry-v2`` is the proposed name. Confirm the destination
   does not already contain another generation before running ``new_catalog``.
4. Create the catalog and inspect its watershed, transposition region, and
   derived valid transposition region. The valid region describes placements
   where the watershed can fit within the transposition region. Confirm it is
   nonempty and scientifically appropriate before the full search.

The resulting layout should keep independent generations::

   catalogs/
     lwi-region3/                         # historical, unchanged
     lwi-region3-deprecated-20260924.zip   # historical archive
     lwi-region3-geometry-v2/              # proposed new generation

Freeze settings and rerun discovery
----------------------------------

``notebooks/catalog_population.ipynb`` currently supplies these settings.
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

Resolve the search-step discrepancy explicitly:
``configs/params-config.json`` says 24 hours, while the population notebook says
6 hours. Record the selected value, all geometry/config hashes, and all export
options in the generation evidence. ``use_valid_region`` affects source DSS
coverage and must also be reviewed, particularly for the full HMS forcing
domain. The search watershed, HMS forcing domain, and RAS coverage domain are
separate concepts; a 5-km target buffer is not proof of model coverage.

For each duration, set ``STORM_DURATION_HOURS`` and call ``new_collection``
through the population notebook with ``SMOKE_TEST=False`` and
``CREATE_NEW_ITEMS=True``. Set ``RUN_DSS_EXPORT=False`` during discovery so a
full export does not start before the new population is reviewed. The API
keyword is ``storm_duration``. Preserve the new ``storm-stats.csv``,
``ranked-storms.csv``, Items, and settings; check temporal search completeness
before accepting the rankings.

Do not reuse historical statistics, ranked tables, or numeric Item directories.
``resume_collection`` only continues a search with the same geometry and
settings. ``workflows/rebuild_ranked_items.py`` reranks existing statistics and
cannot perform a geometry rerun. The notebook's resume cell is an alternative
for an interrupted search, not a required second full-search step. If a resumed
search changes rankings after Items were created, reconcile/rebuild those new
Items before export; never let stale rank-addressed folders select old events.

Validate and export DSS
----------------------

After completing and reviewing a duration's population:

1. Set ``RUN_DSS_EXPORT=True`` and ``DSS_ITEM_IDS=["1"]``; run the export cell
   only. Explicitly select ``collection_id=f"{STORM_DURATION_HOURS}hr-events"``
   and ``DSS_OUTPUT_MODES=("source", "target")``. The API default is source-only.
2. Require both spatial and DSS record-count validations to report ``passed``.
   Inspect source/target placement, SHG-cell offsets, target footprint, hourly
   coverage, and units. Repeat this smoke for every duration.
3. Export the remaining Items. ``DSS_ITEM_IDS=None`` selects the whole
   collection and regenerates the smoke Item too. To retain the successful
   smoke bytes, provide an explicit list of only unexported IDs instead.
4. Check the returned ``failed_count``, per-item statuses, and validation
   reports. Retry only failed/unexported IDs when preserving successful assets.
   The exporter removes and replaces selected DSS outputs; it is not an
   automatic skip-existing/resume mechanism.

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
