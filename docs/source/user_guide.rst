################
Getting Started
################

This section provides a high level overview for using stormhub for production, including creating objects and previewing local catalogs.

Installation
------------

`stormhub` is registered with `PyPI <https://pypi.org/project/stormhub>`_
and can be installed simply using python's pip package installer. Assuming you
have Python already installed and setup:

   .. code-block:: bash

      pip install stormhub


Note that it is highly recommended to create a python `virtual environment
<https://docs.python.org/3/library/venv.html>`_ to install, test, and run
stormhub. It is also recommended to avoid use of Windows Subsystem for Linux (WSL)
as issues can arise with the parallel processing within stormhub.


Starting the server
-------------------

For convenience, a local file server is provided. This server is not necessary for data
production, but is useful for visualizing and exploring the data.

**Start the stormhub file:**

   .. code-block:: bash

      stormhub-server <path-to-local-dir>

The preview binds to ``127.0.0.1`` by default. Stop it with Ctrl+C; HTTP shutdown
and write operations are unavailable. Open a directory containing
``catalog.json`` or ``collection.json`` and click **Open in STAC Browser**.
The link uses the active server port and is intended for the computer running
the server. Restart the server after updating StormHub if the link is absent.
Non-loopback binding requires an explicit
host and ``--allow-network``. Use only trusted content and a directory that other
users cannot modify. Symlinks/junctions resolving outside the root are denied.
This unauthenticated development preview is not a cloud model-library service;
CORS does not provide authentication or authorization.

Troubleshooting STAC Browser
~~~~~~~~~~~~~~~~~~~~~~~~~~~

The hosted viewer is now at `browser.moregeo.it <https://browser.moregeo.it>`_.
The former Radiant Earth URL redirects there. StormHub allows CORS requests
from these two exact origins and links directly to the current viewer.

If the viewer says **The requested page could not be loaded**:

1. Open the reported local request URL directly, for example
   ``http://127.0.0.1:5000/catalog.json``. If it fails, check that the server is
   running and that its selected directory contains the requested file.
2. Stop the server with Ctrl+C and restart it after updating StormHub. An
   already-running process retains the old CORS headers. Reopen **Open in STAC
   Browser** from the local directory listing.
3. If the browser requests local-network access for ``browser.moregeo.it``,
   allow it for this local preview. If previously denied, change that site's
   local-network permission in browser settings and reload the viewer. This
   browser permission is separate from the server's CORS headers.



Local file server is useful for interacting with STAC browser for viewing the data locally. This is not required....

.. figure:: ./images/file-server.png

   Figure: Images stored locally viewed through the browser using a local file server.


Workflows
---------

Catalog command-line workflow
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The ``stormhub`` command and ``python -m stormhub`` expose the same population
workflow as the notebook. Run them in the StormHub scientific environment. The
existing ``stormhub-server`` command is unchanged. To register the new console
command after updating an editable checkout:

.. code-block:: powershell

   python -m pip install --no-deps -e .
   stormhub --help

First create and review a base catalog using the creation notebook. Each command
accepts its directory or ``catalog.json`` and requires an explicit ``--duration``
in hours. Defaults come exclusively from that catalog's frozen
``creation-settings.json``. The editable ``configs/params-config.json`` is not
consulted. CLI options do not rewrite the frozen snapshot.

.. code-block:: powershell

   # Plan a fresh search (no writes or AORC requests).
   stormhub populate catalogs/lwi-region3-geometry-v2 --duration 24 --dry-run
   # Execute that search; DSS export is a separate operation.
   stormhub populate catalogs/lwi-region3-geometry-v2 --duration 24
   # Continue an interrupted full search, with an optional worker override.
   stormhub resume catalogs/lwi-region3-geometry-v2 --duration 24 --workers 4
   # Inspect and then export a selected smoke Item.
   stormhub export-dss catalogs/lwi-region3-geometry-v2 --duration 24 --item-ids 1 --dry-run
   stormhub export-dss catalogs/lwi-region3-geometry-v2 --duration 24 --item-ids 1

Use ``--specific-date 2016-03-08T18:00:00Z`` on ``populate`` for a smoke search;
repeat the option for additional exact event starts. These are individual dates,
not a range. Offset-aware inputs are converted to UTC; unqualified timestamps
are interpreted as UTC. Starts must fall on whole hours. Omitting this option
searches the frozen date range with its saved interval. The date range follows
the existing engine's inclusive midnight endpoints, not the entire final day.

``populate`` always creates a fresh duration workspace. It records resolved
search settings and hashes of the snapshot and domain Items in
``<duration>hr-events/population-settings.json`` before computation. It refuses
an existing directory, including a completed smoke collection. Use a new catalog
generation for a replacement search; there is no destructive population flag.

``resume`` supports a recorded full search with partial ``storm-stats.csv``,
matching settings and domain hashes, and no Items or DSS yet. Worker counts may
change. It refuses smoke searches, legacy searches without the provenance file,
and interruptions after Item creation starts, where reranking could associate
old rank-addressed products with different events. Inspect and reconcile those
runs separately. When no search dates are missing, resume proceeds to ranking
without repeating discovery. Run only one mutating operation per catalog at a
time; the workflow does not provide interprocess locking.

``export-dss`` requires either ``--item-ids 1 2 3`` or ``--all-items``. It uses the
saved source/target modes, grid resolution, target buffer, and valid-region
choice. ``--output-modes target`` or ``--output-modes source target`` explicitly
overrides the modes. Exporting existing selected products requires
``--overwrite``; this replaces selected DSS files and updates their metadata and
the collection manifest. It is not an atomic operation or an automatic
skip-existing resume. Preserve a verified backup before replacing useful
outputs, or select only unexported/failed Items.

All commands accept ``--dry-run`` for read-only validation and JSON plans. A
refusal still fails during a dry run; add ``--overwrite --dry-run`` to inspect a
proposed DSS replacement without performing it. Export prints its per-item JSON
summary. Failures and partial exports return exit code 1; argument errors return
2 and interruption returns 130. ``--traceback`` includes diagnostic tracebacks.
Successful execution does not establish search completeness or engineering
acceptance; review the statistics and retained validation evidence.

The shared Python functions are ``populate_catalog``, ``resume_catalog``, and
``export_catalog_dss`` in ``stormhub.met.catalog_population``. GeoParquet and
normal-precipitation products remain optional notebook/API steps.

Adopting a stopped notebook checkpoint
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``stormhub adopt-checkpoint`` migrates an older search that lacks
``population-settings.json``. It retains completed event statistics and separates
old rank-addressed Items, DSS, rankings, and derived products before resuming.
It does not start computation or stop notebook processes itself.

1. Interrupt the notebook search and verify that its worker processes have
   stopped. Do not run another writer against this catalog during migration.
2. Confirm that **all** retained statistics used the current watershed and
   transposition geometry and selected duration. Review the displayed frozen
   search dates, interval, threshold, and retained count against the actual
   kernel settings; saved notebook source may differ from those settings.
3. Review a dry run and retain its checkpoint fingerprint:

   .. code-block:: powershell

      $plan = stormhub adopt-checkpoint catalogs/lwi-region3-geometry-v2 --duration 72 --dry-run | ConvertFrom-Json
      $plan | ConvertTo-Json -Depth 10

4. Execute against that exact checkpoint, then inspect the resume plan:

   .. code-block:: powershell

      stormhub adopt-checkpoint catalogs/lwi-region3-geometry-v2 --duration 72 --settings-confirmed --writers-stopped --expected-checkpoint-sha256 $plan.checkpoint_sha256
      stormhub resume catalogs/lwi-region3-geometry-v2 --duration 72 --workers 16 --dry-run
      stormhub resume catalogs/lwi-region3-geometry-v2 --duration 72 --workers 16

The operator flags attest historical settings and stopped writers. CSV files
cannot prove which geometry or duration generated their rows. Adoption checks
the exact CSV columns, unique dates within the full search's date/interval grid,
finite numeric values, and ordered precipitation statistics. It refuses malformed
or incomplete rows instead of silently discarding them. Same-geometry smoke rows
may seed the full search when they satisfy this contract.

Before moving anything, adoption writes a file-count/byte-total/SHA-256 inventory
and a ZIP of the complete selected collection, root catalog, frozen settings,
and domain Items. Every ZIP member is read back and verified. It checks source
hashes again before switching, preserves the original directory under
``_checkpoint-adoptions/<unique-id>/retired/``, removes only its root child link,
and installs a statistics-only workspace with an explicitly adopted provenance
record. A checksummed receipt preserves the operator attestations and archive
identity. The new workspace must pass the normal resume preflight.

Caught switch failures roll back the root and old directory. No files are deleted.
An abrupt host/process failure may require manual recovery: stop all writers,
inspect ``transaction.json``, preserve any newly active workspace, restore the
selected directory from ``retired/`` (or the verified ZIP), and restore
``catalog.before.json`` as the root. Never extract the archive over active results.
The ZIP includes the selected collection only; other root child links may refer
to collections outside that backup. Adoption is not an interprocess lock, and
the archive remains on the same disk.

Python API examples
~~~~~~~~~~~~~~~~~~~

A config file shown below includes the information required to create a new catalog.

.. code-block:: json

   {
      "watershed": {
         "id": "indian-creek",
         "geometry_file": "<LOCAL-OR-REMOTE-PATH>/watershed.geojson",
         "description": "Watershed for use in development of storm catalog"
      },
      "transposition_region": {
         "id": "indian-creek-transpo-area-v01",
         "geometry_file": "<LOCAL-OR-REMOTE-PATH>/transposition_region.geojson",
         "description": "Transposition Domain developed by the hydromet team"
      }
   }


The following snippet provides an example of how to build and create a storm catalog. Requires an example watershed and transposition domain (examples available in the `repo <https://github.com/Dewberry/stormhub/tree/main/catalogs/example-input-data>`_).

.. code-block:: python

   from stormhub.logger import initialize_logger
   from stormhub.met.storm_catalog import new_catalog, new_collection

   if __name__ == "__main__":
      initialize_logger()

      # Catalog Args
      root_dir = "<local-path>"
      config_file = f"{root_dir}/duwamish/config.json"
      catalog_id = "duwamish"
      local_directory = f"{root_dir}"

      storm_catalog = new_catalog(
         catalog_id,
         config_file,
         local_directory=local_directory,
         catalog_description="Duwamish Catalog",
      )

      # All Collection Args
      start_date = "1979-02-01"
      end_date = "2024-12-31"
      top_n_events = 440

      # Collection Args
      storm_duration_hours = 48
      min_precip_threshold = 2.5
      storm_collection = new_collection(
         storm_catalog,
         start_date,
         end_date,
         storm_duration_hours,
         min_precip_threshold,
         top_n_events,
         use_threads=True, #True for Linux/WSL, False for Windows
         num_workers=8, # Number of parallel workers
         check_every_n_hours=6,
      )
      # Optionally, add DSS files to storm items
      add_storm_dss_files(storm_catalog)
      # Optionally, create normal precipitation grid
      create_normal_precip(storm_catalog, duration_hours=storm_duration_hours)

.. note::
   If using Windows, set `use_threads` to `False` in order to avoid issues with multiprocessing. On Linux/WSL, set `use_threads` to `True`.
   The use of ProcessPoolExecutor on Linux can lead to complications due to the way processes are spawned. More investigation is needed on this.

Catalog creation from prepared domains
-------------------------------------

``notebooks/catalog_creation.ipynb`` delegates its reusable work to
``stormhub.met.catalog_setup``:

* ``load_catalog_settings`` validates a creation configuration or saved snapshot.
* ``prepare_catalog_domains`` verifies source hashes and prepares WGS84 polygons.
  Each ``PreparedDomain.summary()`` supplies notebook inspection metadata.
* ``plot_catalog_domains`` returns Matplotlib axes for further customization.
* ``prepare_catalog_inputs`` writes a new preparation workspace or verifies
  identical saved inputs for reuse.
* ``create_prepared_catalog`` calls the existing StormHub catalog builder or
  loads an already completed matching catalog without writing it.

There are two deliberately separate configuration roles:

* ``configs/params-config.json`` is the editable input for creating a catalog.
* ``catalogs/<catalog-id>/creation-settings.json`` is the frozen record of that
  catalog's creation settings and the authority for its population defaults.
  Do not edit it to change an existing generation.

The creation notebook writes the snapshot. The population notebook selects
``CATALOG_ID`` explicitly and reads that catalog's snapshot directly, so changing
the editable configuration for a future catalog does not affect an existing one.
The generated runtime config (``catalog-config.json``, or ``lwi-r3-config.json``
in the LWI notebook) separately binds local prepared-domain paths and checksums;
keep it as well.

Catalogs prepared before this rename have a catalog-local ``params-config.json``.
For those preparation workspaces, rename that snapshot to
``creation-settings.json`` without changing its bytes, and verify its SHA-256
before and after. Refuse a conflicting destination rather than overwrite it.
Do not rename the editable file under ``configs/`` or modify preserved historical
catalogs/archives. The new helpers require the explicit snapshot name and do not
fall back to the editable configuration when it is missing.

Set ``EXISTING = "error"`` (the default) to refuse an existing destination.
Set ``EXISTING = "reuse"`` to rerun the notebook against the same generation.
Creation reuse requires matching editable and saved settings/configuration, prepared geometry bytes,
catalog identity, and local domain Items. It does not overwrite changed inputs.
An interrupted AORC base build can be retried from complete verified inputs if
the workspace contains only preparation and expected base-domain files.
Incomplete input writes or unexpected event products require inspection.

For changed geometry or result-defining settings, use a new catalog ID. There
is intentionally no ``overwrite=True`` or delete-and-recreate option in these
helpers. Replacing a referenced catalog under the same path requires a separate
staged build, validation, checksum-complete backup, explicit operator selection,
and rollback procedure. Existing published/historical references should retain
their original catalog identities.

These protections belong to the preparation helpers; the older low-level
``new_catalog`` API retains its existing behavior. The notebook leaves simple
parameter assignments, display expressions, and file listing visible; it no
longer defines reusable functions or assembles configuration files inline.

DSS source and target products
------------------------------

By default, ``add_storm_dss_files`` writes a ``dss-source`` asset at each
storm's original AORC location. To also create a value-preserving transposed
grid at the catalog watershed, request the target product explicitly:

.. code-block:: python

   dss_result = add_storm_dss_files(
      storm_catalog,
      collection_id="24hr-events",
      output_modes=("source", "target"),
      target_buffer_km=5,
      item_ids=["1"],  # Omit to process every item in the collection.
   )

   if dss_result["failed_count"]:
      raise RuntimeError(dss_result["failed_items"])

The ``dss-source`` asset preserves the selected storm's source placement. The
``dss-target`` asset is translated on the 1-km SHG grid and clipped to the
watershed plus ``target_buffer_km``. StormHub records the source and target
centers, snapped SHG-cell offset, residual offset, grid bounds, and validation
status in the STAC asset metadata. Each target item also receives a
``dss-validation`` JSON asset, and the collection receives a CSV DSS manifest.
The returned summary reports the overall status, requested/succeeded/failed
counts, per-item asset hrefs and validation states, failure details, and the
manifest path. Invalid collection IDs or other run-level configuration errors
still raise immediately.

Start with one ``item_ids`` value as a smoke test before processing a full
collection. A target product should only be treated as model-ready when its
spatial and DSS record-count validations both report ``passed``.

For a changed watershed or transposition region, see
:doc:`lwi_geometry_rerun` before reusing a catalog or exporting DSS files.
Geometry changes require new search statistics and rankings. The DSS exporter
replaces selected output files; it does not automatically skip existing exports.

Viewing Results
----------------
Example Collection created for the indian-creek example data.

Collection Views

.. image:: ./images/alt-collection.png

Troubleshooting
----------------

For help troubleshooting, please add an issue on github at `<https://github.com/Dewberry/stormhub/issues>`_
