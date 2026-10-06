"""Offline Journal analysis; findings are evidence candidates, not diagnoses."""

__version__ = "0.1.0"

from .analyzer import ScanOptions, analyze
from .rules import load_rules

__all__ = ["ScanOptions", "analyze", "load_rules", "__version__"]
