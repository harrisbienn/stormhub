NWM land and reservoir state
============================

``stormhub.met.nwm_state`` exports bounded NWM v3.1 CONUS tm00 analysis
state. FloodForecast owns scientific applicability, model state comparison and
reviewed mapping. No HMS parameter assignment or hydraulic datum conversion is
performed here. ``stormhub/nwm-state/0.1.0`` is a research-only owner contract.

``acquire(request, raw_root, offline=False)`` accepts an hourly cycle, product
(``land`` or ``reservoir``), model_version ``v3.1``, sorted lake_ids (empty for
land), HTTPS base_url and explicit retrieval_revision. Conditional requests
retain exact bytes and ETag, observed retrieval time and unknown original
publication time. Each object is limited to 128 MiB; cache ceiling is 512 MiB.
Offline calls authenticate immutable completed receipts without network.

``extract(acquisition, raw_root, output, revision=..., geometry=..., dirty=False)``
authenticates the native file and writes acquisition.json, state.json and
receipt.json with content identities and producer revision. Native model,
product, dimensions, units, packing, reference clock and valid clock must agree.
Analysis initialization is three hours before the tm00 valid/archive cycle.

Land geometry declares ``crs``, ``regions`` and ``partition`` using the existing
fractional precipitation support API. Native x/y centers define a 1000 m lattice;
absolute coordinate quantization is bounded at 0.25 m (not cumulative step
tolerance). The exact coordinate-array hash and normalization rule are exported.
Only selected row segments are read. ``SOILSAT_TOP`` is a dimensionless fraction
over the upper 0.4 m, not a seasonal percentile or an HMS loss parameter.
Every region retains area, valid area, missing fraction and the mean over valid
area. All-missing regions have a null mean. Full sparse support weights,
geometry and grid hashes remain in the owner export.

Reservoir selection uses NHDPlusv2 waterbody IDs, distinct from channel reach
IDs. Inflow/outflow (m3 s-1) and water surface elevation (m) remain separate
quantities; missing values are null. Storage, vertical datum and physical
equivalence are not established by these files. Signed native flows are
preserved within native valid ranges, without inventing a flow direction policy.

The bounded real development sample on November 4, 2026 confirmed these fields
and all 275 active HMS supports; some subbasins have substantial missing land
coverage. Consumers must apply explicit coverage rules. Definitions were checked
against https://water.noaa.gov/about/output_file_contents; exact native metadata
also governs acceptance. Complete integration evidence belongs to FloodForecast
issue 191; source NetCDF and regional exports remain outside Git.
