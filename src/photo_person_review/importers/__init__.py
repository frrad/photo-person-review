"""Source adapters for local photo archives and optional manifests."""

from .catalog import CatalogImportRepository
from .folder import FolderImporter, ImportRepository, ImportResult
from .manifest import ManifestEntry, load_manifest
from .vidigami import VidigamiAdapter

__all__ = [
    "FolderImporter",
    "CatalogImportRepository",
    "ImportRepository",
    "ImportResult",
    "ManifestEntry",
    "VidigamiAdapter",
    "load_manifest",
]
