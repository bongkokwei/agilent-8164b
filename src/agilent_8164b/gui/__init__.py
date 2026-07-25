"""PyQt6 graphical front end for the Agilent/Keysight 8164B laser source."""

from .main_window import LaserMainWindow
from .worker import LaserWorker, SweepConfig

__all__ = ["LaserMainWindow", "LaserWorker", "SweepConfig", "main"]


def main(argv=None) -> int:
    """Entry point for the ``agilent-8164b-gui`` console script."""
    from .app import main as _main

    return _main(argv)
