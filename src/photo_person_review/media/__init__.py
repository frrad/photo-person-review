"""Media inspection helpers.

The importer deliberately stores metadata and references to source files only;
it never copies the image payload into the application's workspace.
"""

from .metadata import ImageMetadata, extract_metadata

__all__ = ["ImageMetadata", "extract_metadata"]
