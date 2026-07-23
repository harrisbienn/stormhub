Scenario Run Contract
=====================

The scenario run contract is the handoff between precipitation preparation,
hydraulic-model automation, quality control, and STAC publication. It records
one planned or completed hydraulic execution without embedding the files
themselves.

The contract complements STAC rather than replacing it:

* STAC Items and Assets provide discovery, spatial metadata, and lineage.
* ``ScenarioRunSpec`` records every input and parameter that can change model
  results.
* ``ScenarioRun`` adds lifecycle state, output files, quality checks, and
  machine-readable failure details.
* A future hydraulic-run STAC Item should include the completed manifest as an
  Asset and link to its precipitation Item with ``derived_from``.

Contract guarantees
-------------------

Version 1.0.0 provides the following guarantees:

* Unknown fields are rejected so misspellings do not silently lose metadata.
* All timestamps are timezone-aware UTC values.
* File inputs and outputs require SHA-256 checksums and byte sizes; the
  transposed DSS and watershed geometry are also checksum-pinned.
* Every file reference has a stable ``asset_key`` used when it is published to
  STAC; keys must be unique within a run.
* Hrefs must be portable relative paths or absolute IRIs, not Windows paths.
* The immutable run specification has a canonical SHA-256 digest.
* Terminal lifecycle states require the timestamps and records needed to
  explain their outcome.
* A successful run requires at least one output and cannot have failed QC.

The ``specification_sha256`` value is an idempotency key. An orchestrator can
detect that an identical specification has already been submitted and avoid an
accidental duplicate HEC-RAS execution. Run status, logs, outputs, and QC are
excluded from this digest because they describe the execution, not its inputs.

Creating a run
--------------

Construct a ``ScenarioRunSpec`` from validated STAC and model references, then
use ``new_scenario_run`` to calculate its identity::

   from datetime import datetime, timezone

   from stormhub.scenarios import ScenarioRunSpec, new_scenario_run, write_scenario_run

   spec = ScenarioRunSpec(
       watershed=watershed,
       precipitation=precipitation,
       hydraulic_model=hydraulic_model,
       execution=execution,
   )
   run = new_scenario_run(spec, created_at=datetime.now(timezone.utc))
   write_scenario_run(run, "runs/scenario-run.json")

``write_scenario_run`` uses an atomic replacement so an interrupted process
does not leave a partially written manifest. To update lifecycle state, build a
new validated ``ScenarioRun`` snapshot; do not mutate a manifest in place.

Building from StormHub STAC
---------------------------

Generated DSS assets include ``file:size`` and the STAC File extension's
``file:checksum`` multihash. The scenario adapter verifies those bytes before a
run is submitted. It also checks that the target DSS was created for the
requested watershed and that DSS validation passed::

   from pathlib import Path

   from stormhub.scenarios import (
       HydraulicModel,
       file_reference_from_path,
       new_scenario_run,
       scenario_run_spec_from_stac,
       write_scenario_run,
   )

   manifest_path = Path("runs") / "pending" / "scenario-run.json"
   model_package = file_reference_from_path(
       "models/lwi-r3-ras.zip",
       manifest_path,
       asset_key="hydraulic-model",
       media_type="application/zip",
       roles=["data", "model"],
   )
   hydraulic_model = HydraulicModel(
       model_id="lwi-r3-ras",
       model_version="2026.07",
       engine_version="6.6",
       plan_name="forecast-plan",
       package=model_package,
   )
   spec = scenario_run_spec_from_stac(
       storm_item,
       watershed_item,
       hydraulic_model,
       execution,
       manifest_path,
   )
   run = new_scenario_run(spec)
   write_scenario_run(run, manifest_path)

By default, ``scenario_run_spec_from_stac`` requires the ``dss-target`` asset,
a ``passed`` DSS validation status, matching ``stormhub:target_watershed_id``,
and a valid local checksum. These are submission gates, not merely descriptive
metadata. Disable them only for explicit migration or diagnostic workflows.

Publishing hydraulic runs
-------------------------

``publish_scenario_run`` writes the final manifest and publishes a STAC Item
into a ``hydraulic-scenario-runs`` Collection::

   import pystac

   from stormhub.scenarios import publish_scenario_run

   catalog = pystac.Catalog.from_file("catalogs/lwi-region3/catalog.json")
   item = publish_scenario_run(
       catalog,
       completed_run,
       manifest_path,
       watershed_item,
   )

The publisher creates the Collection when necessary and writes Items under
``hydraulic-scenario-runs/<run-id>/``. Each Item contains:

* the versioned scenario manifest;
* the watershed-transposed DSS input;
* the versioned hydraulic model package;
* every declared output, such as WSE, depth, velocity, HDF, logs, or reports;
* a ``derived_from`` link to the precipitation scenario; and
* a ``related`` link to the target watershed.

Successful, failed, and cancelled terminal runs can be published. Failed runs
retain their manifest and failure classification even when no hydraulic output
was produced. Planned, queued, and running executions are rejected because
their final provenance is incomplete.

Publication verifies local checksums and sizes by default. A duplicate run ID
is rejected. ``overwrite=True`` permits an idempotent republication only when
the existing Item has the same specification digest; it cannot replace an
existing run with different inputs under the same identity.

Output metadata using ``proj:`` or ``raster:`` fields automatically declares
the corresponding STAC extension on the published Item. This allows gridded
WSE, depth, and velocity products to carry standards-based spatial metadata
without forcing every possible hydraulic product into the core contract.

Schema and compatibility
------------------------

The packaged JSON Schema is
``stormhub/scenarios/schemas/scenario-run-v1.0.0.schema.json``. Non-Python
workers can validate manifests against this file. Python producers should use
the Pydantic models, which are also the source used to generate the schema.

Contract versions follow semantic versioning. Additive optional fields are a
minor change. Removing fields, changing meaning, or making an optional field
required is a major change. Readers should select a model by
``contract_version`` rather than assuming the newest model can parse every
historical manifest.

Extension policy
----------------

Model-specific values belong in the explicit ``parameters``, ``metadata``,
``metrics``, or ``details`` mappings. Stable concepts used by multiple
producers should graduate to typed fields in a later contract version. This
keeps early integration flexible while preventing the top-level contract from
becoming an undocumented bag of keys.
