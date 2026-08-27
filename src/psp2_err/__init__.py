"""Offline PS Vita error-code lookup."""

from .database import Database, ErrorRecord, load_database

__all__ = ["Database", "ErrorRecord", "load_database"]
__version__ = "0.1.0"

