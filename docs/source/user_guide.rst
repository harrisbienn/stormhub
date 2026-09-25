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



Local file server is useful for interacting with STAC browser for viewing the data locally. This is not required....

.. figure:: ./images/file-server.png

   Figure: Images stored locally viewed through the browser using a local file server.


Workflows
---------

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

* ``load_catalog_settings`` validates the shared JSON configuration.
* ``prepare_catalog_domains`` verifies source hashes and prepares WGS84 polygons.
  Each ``PreparedDomain.summary()`` supplies notebook inspection metadata.
* ``plot_catalog_domains`` returns Matplotlib axes for further customization.
* ``prepare_catalog_inputs`` writes a new preparation workspace or verifies
  identical saved inputs for reuse.
* ``create_prepared_catalog`` calls the existing StormHub catalog builder or
  loads an already completed matching catalog without writing it.

Set ``EXISTING = "error"`` (the default) to refuse an existing destination.
Set ``EXISTING = "reuse"`` to rerun the notebook against the same generation.
Reuse requires matching saved settings/configuration, prepared geometry bytes,
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
