"""Regenerate a tiny synthetic MRMS Pass 2 observation-end GRIB2 fixture."""

import json
from pathlib import Path

import numpy as np
import rasterio
from rasterio.io import MemoryFile
from rasterio.shutil import copy
from rasterio.transform import from_origin


def main():
    """Write a known 2x2 hourly mm field with NOAA local product identifiers."""
    root = Path(__file__).parent
    with MemoryFile() as memory:
        with memory.open(
            driver="GTiff",
            height=2,
            width=2,
            count=1,
            dtype="float32",
            crs="EPSG:4326",
            transform=from_origin(-93, 33, 0.01, 0.01),
        ) as dataset:
            dataset.write(np.array([[1, 2], [3, 4]], dtype="float32"), 1)
        copy(
            memory.name,
            root / "pass2.grib2",
            driver="GRIB",
            DISCIPLINE="209",
            IDS_LOCAL_TABLE="1",
            IDS="CENTER=161 SUBCENTER=0 MASTER_TABLE=255 SIGNF_REF_TIME=3 REF_TIME=2025-10-25T18:00:00Z PROD_STATUS=2 TYPE=7",
            PDS_PDTN="0",
            PDS_TEMPLATE_ASSEMBLED_VALUES="6 37 8 0 97 0 0 0 0 102 0 0 255 1 0",
            DATA_ENCODING="IEEE_FLOATING_POINT",
        )
    with rasterio.open(root / "pass2.grib2") as dataset:
        (root / "tags.json").write_text(json.dumps(dataset.tags(1), indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
