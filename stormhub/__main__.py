"""Support ``python -m stormhub`` with a Windows-safe worker entry point."""

from multiprocessing import freeze_support

from stormhub.cli import main


if __name__ == "__main__":
    freeze_support()
    raise SystemExit(main())
