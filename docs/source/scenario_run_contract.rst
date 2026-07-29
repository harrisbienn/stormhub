Scenario Run Contract
=====================

The scenario run contract is the handoff between precipitation preparation,
hydrologic-model automation, hydraulic-model automation, quality control, and
STAC publication. It records one planned or completed integrated execution
without embedding the files themselves.

The contract complements STAC rather than replacing it:

* STAC Items and Assets provide discovery, spatial metadata, and lineage.
* ``ScenarioRunSpec`` records every input and parameter that can change model
  results.
* ``ScenarioRun`` adds lifecycle state, output files, quality checks, and
  machine-readable failure details.
* A published flood-run STAC Item includes the completed manifest as an Asset
  and links to its precipitation Item with ``derived_from``.

Contract guarantees
-------------------

Version 2.1.0 provides the following guarantees:

* Unknown fields are rejected so misspellings do not silently lose metadata.
* All timestamps are timezone-aware UTC values.
* File inputs and outputs require SHA-256 checksums and byte sizes; the
  transposed DSS and watershed geometry are also checksum-pinned.
* Every file reference has a stable ``asset_key`` used when it is published to
  STAC; keys must be unique within a run.
* Hrefs must be portable relative paths or absolute IRIs, not Windows paths.
* The immutable run specification has a canonical SHA-256 digest.
* The workflow explicitly distinguishes direct hydraulic and integrated
  hydrologic-hydraulic runs.
* Integrated runs require a versioned HMS model, a versioned RAS model, and at
  least one explicit HMS-DSS-to-RAS-boundary mapping.
* Stage lifecycle records identify the outputs and QAQC state produced by HMS
  and RAS independently.
* Terminal lifecycle states require the timestamps and records needed to
  explain their outcome.
* A successful run requires at least one output and cannot have failed QC.
* HMS execution, hydrologic handoff, RAS execution, and hydraulic QA/QC are
  separate typed qualification gates.
* Publication disposition is independent from run completion and defaults to
  ``prohibited``.
* ``candidate`` and ``eligible`` publication requires a successful run and a
  ``pass`` result for every workflow-required qualification gate.

The ``specification_sha256`` value is an idempotency key. An orchestrator can
detect that an identical specification has already been submitted and avoid an
accidental duplicate HMS/RAS execution. Run status, stage status, logs,
outputs, and QC are excluded from this digest because they describe the
execution, not its inputs.

Creating a run
--------------

Construct a ``ScenarioRunSpec`` from validated STAC and model references, then
use ``new_scenario_run`` to calculate its identity::

   from datetime import datetime, timezone

   from stormhub.scenarios import ScenarioRunSpec, new_scenario_run, write_scenario_run

   spec = ScenarioRunSpec(
       workflow="hydrologic_hydraulic",
       watershed=watershed,
       precipitation=precipitation,
       hydrologic=hydrologic_stage,
       hydraulic=hydraulic_stage,
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
       HydraulicStageSpec,
       HydrologicStageSpec,
       new_scenario_run,
       scenario_run_spec_from_stac,
       write_scenario_run,
   )

   manifest_path = Path("runs") / "pending" / "scenario-run.json"
   # hms_model and ras_model contain checksum-pinned model package references.
   # verified_boundary_mappings contain exact HMS DSS and RAS boundary IDs.
   hydrologic_stage = HydrologicStageSpec(model=hms_model, execution=hms_execution)
   hydraulic_stage = HydraulicStageSpec(
       model=ras_model,
       execution=ras_execution,
       boundary_mappings=verified_boundary_mappings,
   )
   spec = scenario_run_spec_from_stac(
       storm_item,
       watershed_item,
       hydraulic_stage,
       manifest_path,
       hydrologic=hydrologic_stage,
   )
   run = new_scenario_run(spec)
   write_scenario_run(run, manifest_path)

By default, ``scenario_run_spec_from_stac`` requires the ``dss-target`` asset,
a ``passed`` DSS validation status, matching ``stormhub:target_watershed_id``,
and a valid local checksum. These are submission gates, not merely descriptive
metadata. Disable them only for explicit migration or diagnostic workflows.

Publishing integrated flood runs
--------------------------------

``publish_scenario_run`` writes the final manifest and publishes a STAC Item
into a ``flood-scenario-runs`` Collection::

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
``flood-scenario-runs/<run-id>/``. Each Item contains:

* the versioned scenario manifest;
* the watershed-transposed DSS input;
* the versioned hydrologic model package when present;
* the versioned hydraulic model package;
* every declared output, such as WSE, depth, velocity, HDF, logs, or reports;
* a ``derived_from`` link to the precipitation scenario; and
* a ``related`` link to the target watershed.

Successful, failed, and cancelled terminal runs can be published. Failed runs
retain their manifest and failure classification even when no hydraulic output
was produced. Planned, queued, and running executions are rejected because
their final provenance is incomplete.

Catalog registration and forecast promotion are deliberately separate.
Terminal Items may use these publication dispositions:

* ``prohibited`` - retained as evidence and excluded from forecast guidance;
* ``internal`` - available for engineering review only;
* ``candidate`` - all automated gates passed and scientific review is pending;
* ``eligible`` - explicitly approved for scenario-matching forecast use; and
* ``retired`` - retained for provenance and excluded from new selection.

The publisher exposes the four gate states, disposition, and a derived
``stormhub:forecast_eligible`` Boolean as searchable Item properties. A
failed, cancelled, conditional, or unevaluated run cannot be promoted merely
because its files exist.

Publication verifies local checksums and sizes by default. A duplicate run ID
is rejected. ``overwrite=True`` permits an idempotent republication only when
the existing Item has the same specification digest; it cannot replace an
existing run with different inputs under the same identity.

Output metadata using ``proj:`` or ``raster:`` fields automatically declares
Projection 2.0 or the corresponding Raster extension on the published Item.
Projection 2.0 preserves package-produced ``proj:code`` values even when
StormHub is running on its pinned PySTAC 1.10/STAC 1.0 stack. This allows gridded
WSE, depth, and velocity products to carry standards-based spatial metadata
without forcing every possible hydraulic product into the core contract.
Validated ``table:columns`` and ``table:row_count`` metadata similarly declares
the Table extension for hydrographs. Partial Table metadata is rejected by the
contract. The Datacube extension is not declared unless a future producer
supplies a separately validated time-cube contract.

The Collection declares the expected scenario-response Asset union through
``item_assets``. Stable keys include:

.. code-block:: text

   scenario-run
   scenario-products
   qualification
   target-precipitation-dss
   hms-output-dss
   hms-pathname-catalog
   hms-hydrographs
   ras-result-hdf
   ras-hydrographs
   maximum-wse
   maximum-depth
   maximum-velocity
   hydraulic-time-cube
   preview

Assets that do not exist for a particular run are omitted from its Item. The
Collection definition is the union of possible assets, not a claim that every
run produced every asset. The publisher also retains definitions for observed
model-package or extension assets so custom package keys remain discoverable.

The source precipitation Item's ``dss-target`` key and the response catalog's
``target-precipitation-dss`` key serve different scopes. The response Asset
records the original source key in ``stormhub:source_asset_key`` and keeps the
Item-level ``derived_from`` link. Publication does not edit the source Item.

Schema and compatibility
------------------------

The current packaged JSON Schema is
``stormhub/scenarios/schemas/scenario-run-v2.1.0.schema.json``. Historical
``scenario-run-v1.0.0.schema.json`` and
``scenario-run-v2.0.0.schema.json`` files remain packaged for readers of
previously published manifests. Non-Python
workers can validate manifests against this file. Python producers should use
the Pydantic models, which are also the source used to generate the schema.

Contract versions follow semantic versioning. Additive optional fields are a
minor change. Removing fields, changing meaning, or making an optional field
required is a major change. Readers should select a model by
``contract_version`` rather than assuming the newest model can parse every
historical manifest.

Version 2.1.0 is additive over 2.0.0. The Python reader accepts a 2.0.0
manifest and supplies conservative qualification defaults: every gate is
``not_evaluated`` and publication is ``prohibited``. New manifests emit
2.1.0.

Version 2.0.0 was a deliberate breaking change from the single hydraulic-stage
1.0.0 model. ``ScenarioRunSpec`` now groups model and execution provenance into
``hydrologic`` and ``hydraulic`` stage specifications and records an explicit
``workflow``.

Extension policy
----------------

Model-specific values belong in the explicit ``parameters``, ``metadata``,
``metrics``, or ``details`` mappings. Stable concepts used by multiple
producers should graduate to typed fields in a later contract version. This
keeps early integration flexible while preventing the top-level contract from
becoming an undocumented bag of keys.
