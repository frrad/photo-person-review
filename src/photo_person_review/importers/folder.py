"""Incremental, source-read-only folder importer."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Protocol

from photo_person_review.media.metadata import ImageMetadata, extract_metadata

from .manifest import ManifestEntry, load_manifest

SUPPORTED_IMAGE_EXTENSIONS = frozenset(
    {
        ".avif",
        ".bmp",
        ".cr2",
        ".cr3",
        ".dng",
        ".gif",
        ".heic",
        ".heif",
        ".jpeg",
        ".jpg",
        ".nef",
        ".png",
        ".raw",
        ".tif",
        ".tiff",
        ".webp",
    }
)


@dataclass(frozen=True)
class PreviousObservation:
    """Last observation for a source-relative path."""

    relative_path: str
    content_hash: str | None
    byte_size: int | None = None
    modified_ns: int | None = None
    photo_id: str | None = None
    observation_state: str = "present"


@dataclass(frozen=True)
class ImportObservation:
    relative_path: str
    status: str
    metadata: ImageMetadata | None = None
    error: str | None = None


class ImportRepository(Protocol):
    """Minimal persistence boundary used by :class:`FolderImporter`.

    Implementations should make ``record_observation`` append-only.  The
    stable photo row is deduplicated by ``content_hash`` in ``upsert_photo``;
    observations and batch links are deliberately recorded on every run.
    """

    def begin_import_run(self, *, source_id: str, root_path: str, started_at: str) -> str: ...

    def observations_for_source(self, *, source_id: str) -> Iterable[PreviousObservation]: ...

    def upsert_photo(
        self,
        *,
        metadata: ImageMetadata,
        external_refs: Mapping[str, Any],
        provider_hints: Mapping[str, Any],
    ) -> str: ...

    def record_observation(
        self,
        *,
        run_id: str,
        source_id: str,
        photo_id: str,
        relative_path: str,
        status: str,
        metadata: ImageMetadata,
        batch_id: str,
        external_refs: Mapping[str, Any],
        provider_hints: Mapping[str, Any],
    ) -> None: ...

    def record_missing(
        self,
        *,
        run_id: str,
        source_id: str,
        relative_path: str,
        previous: PreviousObservation,
    ) -> None: ...

    def create_batch(self, *, run_id: str, source_id: str, batch_key: str) -> str: ...

    def finish_import_run(self, *, run_id: str, summary: Mapping[str, Any]) -> None: ...


@dataclass
class ImportResult:
    run_id: str
    source_id: str
    observations: list[ImportObservation] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    errors: list[ImportObservation] = field(default_factory=list)
    batches: dict[str, str] = field(default_factory=dict)

    @property
    def counts(self) -> dict[str, int]:
        result = {
            "new": 0,
            "unchanged": 0,
            "replaced": 0,
            "missing": len(self.missing),
            "errors": len(self.errors),
        }
        for observation in self.observations:
            result[observation.status] = result.get(observation.status, 0) + 1
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "source_id": self.source_id,
            "counts": self.counts,
            "observations": [
                {"relative_path": item.relative_path, "status": item.status, "error": item.error}
                for item in self.observations
            ],
            "missing": self.missing,
            "errors": [
                {"relative_path": item.relative_path, "status": item.status, "error": item.error}
                for item in self.errors
            ],
            "batches": self.batches,
        }


def _relative_path(root: Path, path: Path) -> str:
    return PurePosixPath(path.relative_to(root).as_posix()).as_posix()


def iter_image_paths(root: str | os.PathLike[str]) -> Iterable[Path]:
    """Yield regular image files below *root*, never following symlinks."""

    base = Path(root).expanduser().resolve(strict=True)
    if not base.is_dir() or base.is_symlink():
        raise ValueError(f"import root is not a real directory: {root}")
    for directory, dirnames, filenames in os.walk(base, followlinks=False):
        # os.walk can still list symlinked directories; remove them before
        # recursion, and skip symlinked files as well.
        dirnames[:] = sorted(name for name in dirnames if not (Path(directory) / name).is_symlink())
        for name in sorted(filenames):
            candidate = Path(directory) / name
            if candidate.is_symlink() or not candidate.is_file():
                continue
            if candidate.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS:
                yield candidate


def _manifest_map(root: Path, entries: Iterable[ManifestEntry]) -> dict[str, ManifestEntry]:
    result: dict[str, ManifestEntry] = {}
    for entry in entries:
        if not entry.path:
            continue
        candidate = Path(entry.path)
        if candidate.is_absolute():
            try:
                key = _relative_path(root, candidate.resolve(strict=False))
            except ValueError:
                continue
        else:
            key = PurePosixPath(entry.path.replace("\\", "/")).as_posix().lstrip("./")
        if key in result:
            raise ValueError(f"manifest contains duplicate path: {key}")
        result[key] = entry
    return result


class FolderImporter:
    """Import a folder repeatedly while retaining only metadata and links."""

    def __init__(
        self,
        repository: ImportRepository,
        *,
        metadata_reader: Callable[[Path], ImageMetadata] = extract_metadata,
    ) -> None:
        self.repository = repository
        self.metadata_reader = metadata_reader

    def import_folder(
        self,
        root: str | os.PathLike[str],
        *,
        source_id: str | None = None,
        manifest: str | os.PathLike[str] | None = None,
        manifest_entries: Iterable[ManifestEntry] | None = None,
    ) -> ImportResult:
        base = Path(root).expanduser().resolve(strict=True)
        if not base.is_dir() or base.is_symlink():
            raise ValueError(f"import root is not a real directory: {root}")
        source = source_id or f"folder:{base}"
        entries = (
            list(manifest_entries)
            if manifest_entries is not None
            else (load_manifest(Path(manifest)) if manifest else [])
        )
        by_path = _manifest_map(base, entries)
        started_at = datetime.now(timezone.utc).isoformat()
        run_id = self.repository.begin_import_run(source_id=source, root_path=str(base), started_at=started_at)
        prior = {item.relative_path: item for item in self.repository.observations_for_source(source_id=source)}
        result = ImportResult(run_id=run_id, source_id=source)
        seen: set[str] = set()

        for path in iter_image_paths(base):
            relative = _relative_path(base, path)
            seen.add(relative)
            try:
                metadata = self.metadata_reader(path)
                old = prior.get(relative)
                if old is None:
                    status = "new"
                elif old.content_hash == metadata.content_hash and old.observation_state == "present":
                    status = "unchanged"
                elif old.content_hash == metadata.content_hash:
                    # The same content returned after a deletion is a new
                    # observation even though its stable photo row is reused.
                    status = "new"
                else:
                    status = "replaced"
                entry = by_path.get(relative, ManifestEntry(path=relative))
                if entry.capture_time and metadata.captured_at_source == "file_mtime":
                    # Provider capture time supplements missing EXIF without
                    # mutating the source file.
                    metadata = ImageMetadata(
                        **{
                            **metadata.to_dict(),
                            "captured_at": entry.capture_time,
                            "captured_at_source": "manifest",
                        }
                    )
                batch_key = metadata.capture_date or "undated"
                batch_id = result.batches.get(batch_key)
                if batch_id is None:
                    batch_id = self.repository.create_batch(run_id=run_id, source_id=source, batch_key=batch_key)
                    result.batches[batch_key] = batch_id
                photo_id = self.repository.upsert_photo(
                    metadata=metadata,
                    external_refs={
                        **({"external_id": entry.external_id} if entry.external_id else {}),
                        **entry.external_refs,
                    },
                    provider_hints=entry.provider_hints,
                )
                self.repository.record_observation(
                    run_id=run_id,
                    source_id=source,
                    photo_id=photo_id,
                    relative_path=relative,
                    status=status,
                    metadata=metadata,
                    batch_id=batch_id,
                    external_refs={
                        **({"external_id": entry.external_id} if entry.external_id else {}),
                        **entry.external_refs,
                    },
                    provider_hints=entry.provider_hints,
                )
                result.observations.append(ImportObservation(relative, status, metadata))
            except Exception as exc:  # one corrupt file must not abort a batch
                failure = ImportObservation(relative, "error", error=f"{type(exc).__name__}: {exc}")
                result.errors.append(failure)

        for relative, old in sorted(prior.items()):
            if relative in seen:
                continue
            self.repository.record_missing(run_id=run_id, source_id=source, relative_path=relative, previous=old)
            result.missing.append(relative)
        self.repository.finish_import_run(run_id=run_id, summary=result.counts)
        return result

    # Friendly alias for callers that use ``run`` as the importer operation.
    run = import_folder
