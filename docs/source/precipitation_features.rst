Comparable rainfall features
============================

``stormhub/precipitation-features/0.1.0`` is a research contract for total
target and incoming rainfall. StormHub owns extraction; FloodForecast owns
matching, confidence, response joins, qualification, and promotion. Exporting
features grants no forecast eligibility. This is separate from the frozen
historical scenario-run contract.

One calculation path
--------------------

``stormhub.met.precipitation_features.extract_features`` accepts normalized
interval-depth grids, exact source identity, spatial support, and extractor
provenance. The module requires only the standard library. The DSS and JSON
adapters in ``precipitation_adapters`` and the HRRR adapter in
``hrrr_normalization`` feed this same calculator. Native adapters normalize
units, clocks, row order, interval accumulation, and missingness before using
it. NWM streamflow is not a rainfall input.

Bounded HRRR research forcing
----------------------------

``python -m stormhub.met.hrrr_cli`` accepts ``--request``, ``--grid``,
``--support``, ``--cache``, ``--output`` and explicit ``--revision`` inputs.
The request JSON has exactly these fields::

    {"initialization":"2025-10-25T18:00:00Z","hours":24,
     "model_version":"hrrr-v4",
     "base_url":"https://noaa-hrrr-bdp-pds.s3.amazonaws.com"}

Grid/support files use the existing contracts above. ``--offline`` requires
a retained complete acquisition and verifies every source checksum before
normalization. Use a new output directory for a changed code revision or input;
existing artifacts cannot be replaced with different content. The CLI records
``--dirty`` when explicitly supplied; the calling orchestrator must verify the
actual checkout. Neither this adapter nor catalog registration grants eligibility.

Only CONUS surface ``wrfsfc`` hourly APCP is supported. The 00/06/12/18 UTC
cycles allow up to 48 hours; other cycles allow 18. The adapter selects one
hourly record per valid interval, then independently checks GRIB2 template 4.8,
surface, parameter, cycle, generating process, units, statistical process and
duration through GDAL. Cumulative records are never differenced or substituted.
Missing hours, duplicate hourly records, mixed grids/cycles, negative rainfall
and missing values in consumed cells fail closed. A failed acquisition has no
completion receipt. The GRIB decoder uses existing Rasterio/GDAL dependencies.

Acquisition uses HTTPS inventories and conditional byte-range requests pinned
to a strong ETag. Each retained inventory/message has a SHA256 and byte count.
Transient failures receive at most three attempts; changed objects fail. Default
limits are 8 MiB per message, 64 MiB per request and 256 MiB retained cache;
the Python API permits explicit budgets up to a 1 GiB cache. A fresh request
retains only selected messages and inventories, not whole CONUS products.
The caller owns cache retention/eviction; no automatic deletion occurs.

Conservative remapping intersects projected source-cell polygons with the
target cells required by the support. Each source edge has four segments.
The projected partition must cover each selected cell to 1e-7 relative area;
interval common-footprint volume accounting must agree within max(1e-6 m3,
1e-10 times source volume). These checks establish numerical bookkeeping,
not meteorological accuracy. Remapping assumes constant depth inside each
native cell; subcell gradients and projection-edge approximation remain
research limitations. Limits are 250,000 target grid cells, 50,000 selected
target cells and 100,000 cells in the native read window. Outside selected
support cells remain null. Region aggregation uses the shared calculator.

``stormhub/forcing-dataset/0.1.0`` binds the immutable acquisition receipt,
provider/model declaration, initialization and valid intervals, raw GRIB tags,
normalized artifact, weights, support, features, producer code/dependencies,
and volume checks. ``forcing_id`` binds acquisition ID, normalized descriptor,
support hash and producer; ``dataset_id`` hashes the complete receipt excluding
itself. Artifact descriptors resolve within the output directory; descriptors
inside acquisition resolve within the explicitly bound raw cache.

``observed_at`` means this client retrieved the message at that instant.
Object Last-Modified is recorded separately and does not prove original
publication time. Archive replay therefore has no operational issue time or
verified as-of hindcast vintage. APCP is liquid-water equivalent; no snow
storage/melt, ongoing-event observations, or NWM state is included. The receipt
is explicitly research-only and ``forecast_eligible=false``.

Primary provider specifications:
`NOAA HRRR products <https://www.nco.ncep.noaa.gov/pmb/products/hrrr/>`_,
`NOAA GRIB2 template 4.8 <https://www.nco.ncep.noaa.gov/pmb/docs/grib2/grib2_doc/grib2_temp4-8.shtml>`_,
and `GDAL GRIB metadata <https://gdal.org/en/stable/drivers/raster/grib.html>`_.
The synthetic saved GRIB fixtures under ``tests/fixtures/hrrr`` have known
hourly totals and a reproducible generator; native tests replay them offline.

The normalized contract is ``stormhub/normalized-precipitation/0.1.0``:

* ``grid``: CRS, positive ``shape`` [rows, columns], lower-left ``origin``
  [x, y], square ``cell_size_meters``, and ``row_order=south_to_north``.
* ``event_start`` and ``event_end``: explicit UTC instants delimiting a complete
  event; ``interval_minutes``: one positive, whole-minute interval.
* ``units``: ``mm`` or ``in``; ``measurement=interval-depth``. Every row in
  ``values`` is one flattened grid, south to north, west to east. There must
  be exactly one row per interval. ``null`` is missing; zero is observed dry
  rainfall. Rates, cumulative totals, negative values, and NaN are rejected.

Do not transpose an already targeted DSS. Do not substitute original source
rainfall, HMS excess rainfall, or the model's zero-padded hydraulic tail.
Intermediate grids may replace DSS only after equivalence is demonstrated.

Spatial support
---------------

``build_spatial_support(grid, regions, region_crs=..., partition=...)``
intersects valid GeoJSON polygons with projected metre grid cells. Each region
has an explicit ``id``, ``kind`` (basin, subbasin, domain, or application-area),
and geometry. Invalid polygons are rejected rather than silently repaired.
An optional parent/children partition must cover its parent without overlaps.
No scientific equivalence is implied between a ranking domain, HMS basin,
and RAS application area.

The resulting ``stormhub/precipitation-support/0.1.0`` records the grid hash,
region identities, canonical projected geometry hashes, full polygon areas,
sparse cell-intersection areas, and partition. Regions extending beyond the
grid retain their full area, so incomplete coverage cannot become a complete
rainfall estimate. Retain this support artifact beside its geometry evidence;
the feature manifest stores its hash, not all weights. Rebuild support for a
changed grid or geometry. Existing center-in-cell transfer masks are not
fractional polygon intersections and must not be silently reused.

Descriptors and missingness
---------------------------

For region area A and intersection areas a, each interval volume is
sum(depth_mm * a) / 1000 cubic metres; complete mean depth is volume * 1000 / A.
Subbasin means are not averaged without area weights. Exports retain:

* interval depth, observed partial volume, and covered-area fraction;
* total depth and volume only when every interval covers the entire region;
* supported rolling 60-, 180-, and 360-minute maximum depths and elapsed
  end times (earliest tie); windows not divisible by the native interval or
  longer than the event are explicitly unsupported;
* interval fractions of total rainfall and the rainfall-weighted centroid
  using interval midpoints;
* child volume fractions of a complete, positive parent total.

Incomplete regions have null totals, maxima, and temporal fractions. Dry
events retain zero totals; temporal fractions, centroid, and allocation are
null because a zero denominator is undefined. These are descriptors, not
probabilities or confidence estimates. The feature definition and its hash
identify these choices. A changed definition requires new features.

Identity and reuse
------------------

``source`` records collection, Item, asset key, exact SHA256, byte size, and
``target-precipitation`` or ``incoming-precipitation`` role. File adapters
authenticate actual bytes. The pure calculator trusts this explicit boundary.
``extractor`` records name, version, full Git revision, dirty status, code hash,
and relevant dependency versions. The code hash normalizes source line endings.
The caller must supply the revision/dirty state of the actual extractor checkout;
the CLI does not infer provenance from the current working directory.

The manifest contains grid, region summaries, support/definition/extractor
hashes, absolute UTC event bounds, elapsed interval ends, normalized-values
hash, descriptors, and completeness. Its ``feature_id`` is the first 24 hex
characters of SHA256 over canonical JSON excluding ``feature_id`` itself.
Canonical JSON sorts keys, uses compact separators, rejects nonfinite numbers,
and encodes Unicode directly as UTF-8 (``ensure_ascii=False``). The values hash
streams one canonical normalized-mm row plus a newline per interval.

``write_features`` creates an immutable content-named JSON file. Identical
retries succeed; different existing bytes fail. Consumers should additionally
pin the full manifest byte checksum. Changed source bytes, geometry, support,
definition, or extractor produce a new identity. Static and incoming paths
can have equal descriptors while retaining different source/feature identities.

DSS boundary
------------

``read_dss`` verifies and copies the source before opening it with hecdss, then
verifies the source again. Native readers never open the canonical asset.
Selectors must resolve one PRECIPITATION series with PER-CUM data type,
explicit D/E interval bounds, compatible UTC metadata, consistent grid/units,
and contiguous coverage. Gaps, duplicate intervals, mixed series, unsupported
units, and ambiguous clocks fail. Both ordinary midnight and DSS 2400 notation
are supported. The historical writer leaves ``isInterval=0`` and
``nullValue=0``; interval semantics come from PER-CUM and D/E bounds, and dry
zero cells stay valid. The HEC missing sentinel and nonfinite native values
become null. Unsupported negative depths fail in the calculator.

Reproduce the small fixture
---------------------------

The JSON fixtures under ``tests/fixtures/precipitation`` contain two cells,
three hourly intervals, and a fractional-cell basin partition. Expected basin
volume is 14,000 m3, and child allocations are 3/7 and 4/7. The exported fixture
uses deliberately synthetic provenance; it is not engineering evidence.

From the owning environment::

    python -I -m stormhub.met.precipitation_features_cli --format normalized-json --input tests/fixtures/precipitation/normalized.json --source tests/fixtures/precipitation/source.json --support tests/fixtures/precipitation/support.json --revision <full-stormhub-sha> --output <local-output-directory>

For DSS use ``--format dss`` and add ``--selector``, ``--start`` and ``--end``.
Support must match the actual DSS grid, including origin and shape. Do not
assume every target asset in a collection uses one grid.

Portable unit checks::

    python -I -m pytest -q tests/test_precipitation_features.py

Optional native/geometry checks, with existing scientific dependencies::

    python -I -m pytest -q tests/test_precipitation_native.py

The native test creates a tiny real DSS in temporary storage. Neither test
suite requires installed HEC-HMS or HEC-RAS or large production assets.
