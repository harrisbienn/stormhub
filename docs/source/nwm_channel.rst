NWM channel research boundary
=============================

``stormhub.met.nwm_channel`` owns bounded acquisition and native NetCDF4
decoding for CONUS NWM v3.0 ``analysis_assim`` tm00 and ``short_range`` f001
through f018. Install ``stormhub[nwm]`` for the optional h5netcdf/h5py reader;
these dependencies are not needed by portable contracts or FloodForecast.

An explicit request contains ``cycle``, ``product``, ``hours`` (zero for
analysis), sorted unique ``reach_ids`` (1-100), ``model_version='v3.0'``,
``network='NHDPlusv2-CONUS'``, HTTPS ``base_url`` and ``retrieval_revision``.
``acquire(request, raw_root, offline=False)`` retains full source objects with
strong ETag conditional GETs, checksums and retrieval times. Limits are 32 MiB
per object, 18 objects per forecast and 768 MiB per retained cache. Native
decoding reads at most three million reach IDs, selecting only requested flow
values. A new retrieval revision creates new evidence; no eviction is automatic.

``extract(acquisition, raw_root, output, revision=<40-character Git SHA>)``
exports ``stormhub/nwm-channel-series/0.1.0`` with authenticated ``series.json``
and ``acquisition.json``. Extraction checks model/configuration/network metadata,
native clock coordinates, unique reach identity, streamflow units, fill values,
valid range and scale/offset before producing finite nonnegative m3/s samples.
The exact metadata structure is deliberately narrow: a future model or
configuration requires a separately tested adapter change.

The archive 18Z analysis has a 15Z model initialization and an 18Z valid time;
an 18Z short-range forecast initializes at 18Z. The decoder preserves these
different clocks. Samples are instantaneous; hourly spacing is not an hourly
mean-flow or accumulation declaration. Analysis and forecast remain separate
datasets, never joined into a continuous hydrograph by this adapter.

The native file identifies NHDPlusv2 CONUS ComIDs, but contains no RouteLink
artifact hash or reviewed HMS crosswalk. These remain unknown. FloodForecast
owns contributing-area/location equivalence, event alignment and comparisons.
``forecast_eligible`` remains false. Retrieval time and S3 Last-Modified do not
prove operational availability at a historical issue time. HRRR/MRMS forcing
is shared with NWM and cannot count as independent validation; channel flow
does not establish HMS loss parameters or soil moisture.

Sources: `NOAA NWM configurations <https://water.noaa.gov/about/nwm>`_,
`NCEP filename conventions <https://www.nco.ncep.noaa.gov/pmb/products/nwm/>`_,
and the retained native NetCDF metadata. The NetCDF global and coordinate
clocks, not filenames alone, determine acceptance.

Tests: ``python -I -m pytest tests/test_nwm_channel.py tests/test_nwm_native.py``.
The native tests create tiny synthetic packed NetCDF4 files; no NOAA download
or engineering application is needed.
