"""Python driver for the Keysight/Agilent 8164B Lightwave Measurement System."""

import logging

from .instrument import Agilent8164B

# Library convention: no output unless the application configures logging.
logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = ["Agilent8164B"]

__version__ = "0.1.0"
