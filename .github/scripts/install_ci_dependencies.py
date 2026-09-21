"""Install the portable CI subset from package metadata, avoiding duplicate pins."""

from importlib.metadata import requires
import subprocess
import sys

from packaging.requirements import Requirement

selected = {"requests", "pystac", "shapely", "pydantic"}
requirements = [value for value in requires("stormhub") or [] if Requirement(value).name in selected]
if {Requirement(value).name for value in requirements} != selected:
    raise SystemExit("CI dependency selection no longer matches package metadata")
subprocess.run([sys.executable, "-m", "pip", "install", *requirements], check=True)
