"""Read-only adapter for Vidigami's hash-named archive and media report."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .folder import FolderImporter, ImportRepository, ImportResult, iter_image_paths
from .manifest import ManifestEntry, load_manifest


class VidigamiAdapter:
    """Map report ``media_id`` values to privacy-preserving archive names.

    The adapter only reads the supplied archive and report.  It intentionally
    does not inspect Vidigami configuration, credentials, SQLite, or network
    endpoints.
    """

    def __init__(self, repository: ImportRepository) -> None:
        self.repository = repository

    @staticmethod
    def archive_stem(media_id: str) -> str:
        return hashlib.sha256(media_id.encode("utf-8")).hexdigest()

    def import_archive(
        self,
        archive: str | Path,
        report: str | Path,
        *,
        source_id: str = "vidigami",
    ) -> ImportResult:
        root = Path(archive).expanduser().resolve(strict=True)
        if not root.is_dir() or root.is_symlink():
            raise ValueError(f"archive is not a real directory: {archive}")
        report_entries = load_manifest(report)
        by_stem: dict[str, Path] = {}
        for path in iter_image_paths(root):
            stem = path.stem.lower()
            if stem in by_stem:
                raise ValueError(f"archive has duplicate hash stem: {stem}")
            by_stem[stem] = path

        mapped: list[ManifestEntry] = []
        missing: list[str] = []
        for row in report_entries:
            if not row.external_id:
                missing.append("<row without media_id>")
                continue
            expected = self.archive_stem(row.external_id)
            candidate = by_stem.get(expected)
            if candidate is None:
                missing.append(row.external_id)
                continue
            hints: dict[str, Any] = dict(row.provider_hints)
            hints["source"] = "vidigami"
            mapped.append(
                ManifestEntry(
                    path=candidate.relative_to(root).as_posix(),
                    external_id=row.external_id,
                    capture_time=row.capture_time,
                    external_refs={**row.external_refs, "media_id": row.external_id},
                    provider_hints=hints,
                )
            )
        if missing:
            raise ValueError(f"Vidigami report rows without matching archive files: {len(missing)}")
        result = FolderImporter(self.repository).import_folder(
            root,
            source_id=source_id,
            manifest_entries=mapped,
        )
        return result
