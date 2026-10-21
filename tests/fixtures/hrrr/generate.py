"""Regenerate tiny synthetic GRIB2 fixtures with explicitly known hourly depths.

Requires Rasterio/GDAL. These are synthetic HRRR-shaped metadata, not NOAA data.
The second interval doubles the first; each 3 km cell has known interval depth.
"""

import json
from pathlib import Path

import numpy as np
import rasterio
from rasterio.io import MemoryFile
from rasterio.shutil import copy
from rasterio.transform import from_origin


def main():
    """Write two tiny GRIB2 messages and portable decoder metadata."""
    root = Path(__file__).parent
    crs = "+proj=lcc +lat_1=38.5 +lat_2=38.5 +lat_0=38.5 +lon_0=-97.5 +R=6371229 +units=m"
    for hour in (1, 2):
        pds = f"1 8 2 0 83 0 0 1 {hour - 1} 1 0 0 255 0 0 2025 10 25 {18 + hour} 0 0 1 0 1 2 1 1 255 0"
        with MemoryFile() as mem:
            with mem.open(
                driver="GTiff",
                height=2,
                width=2,
                count=1,
                dtype="float32",
                crs=crs,
                transform=from_origin(0, 6000, 3000, 3000),
            ) as dataset:
                dataset.write(np.array([[1, 2], [3, 4]], dtype="float32") * hour, 1)
            copy(
                mem.name,
                root / f"f{hour:02d}.grib2",
                driver="GRIB",
                DISCIPLINE="0",
                IDS="CENTER=7 SUBCENTER=0 MASTER_TABLE=2 SIGNF_REF_TIME=1 REF_TIME=2025-10-25T18:00:00Z PROD_STATUS=0 TYPE=1",
                PDS_PDTN="8",
                PDS_TEMPLATE_ASSEMBLED_VALUES=pds,
                DATA_ENCODING="IEEE_FLOATING_POINT",
            )
        with rasterio.open(root / f"f{hour:02d}.grib2") as dataset:
            (root / f"f{hour:02d}-tags.json").write_text(json.dumps(dataset.tags(1), indent=2) + "\n")


if __name__ == "__main__":
    main()
